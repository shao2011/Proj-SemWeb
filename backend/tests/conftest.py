"""Postgres test harness.

Every test gets its own schema on a real Postgres, created before the test and
dropped after. That keeps tests isolated without the cost of a database per
test, and it means the suite exercises the same engine that ships rather than a
SQLite stand-in whose dialect differs.

Requires the `db` service from compose.yaml:

    docker compose up -d db

Point somewhere else with TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, event, text

from rag_ingestion.config import DEFAULT_DATABASE_URL
from rag_ingestion.db.base import target_metadata
from rag_ingestion.db.session import build_engine
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


def pytest_configure(config: pytest.Config) -> None:
    """Fail loudly and early rather than once per database test."""
    probe = build_engine(TEST_DATABASE_URL, poolclass=None)
    try:
        with probe.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # noqa: BLE001 - surfaced as a pytest error
        raise pytest.UsageError(
            f"Cannot reach Postgres at {TEST_DATABASE_URL!r}.\n"
            "Start it with:  docker compose up -d db\n"
            f"Underlying error: {type(error).__name__}: {error}"
        ) from error
    finally:
        probe.dispose()


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    schema = f"test_{uuid.uuid4().hex[:16]}"
    admin = build_engine(TEST_DATABASE_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = build_engine(TEST_DATABASE_URL)

    # `SET` on each new connection rather than `-csearch_path=` in the startup
    # packet. A connection pooler (Supabase's Supavisor, pgbouncer) drops the
    # `options` parameter, and the engine then silently ran in `public`, so
    # every test saw every other test's rows. A plain statement survives the
    # pooler, and behaves identically on a direct connection.
    @event.listens_for(engine, "connect")
    def _apply_search_path(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    target_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def project_store(pg_engine: Engine) -> PostgresProjectStore:
    return PostgresProjectStore(pg_engine)


@pytest.fixture
def thread_store(pg_engine: Engine) -> PostgresThreadStore:
    return PostgresThreadStore(pg_engine)
