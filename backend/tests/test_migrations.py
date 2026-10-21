"""The Alembic chain, run against a real empty database.

This is the gap that let a revision ship with an empty `upgrade()`: every other
test builds its schema with `target_metadata.create_all()`, which never touches
a migration, so the whole suite stayed green while `alembic upgrade head` was
broken. These tests run the chain itself.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event, inspect, text

from rag_ingestion.db.base import target_metadata
from rag_ingestion.db.session import build_engine
from tests.conftest import TEST_DATABASE_URL

MIGRATED_TABLES = {
    "projects",
    "project_files",
    "project_members",
    "threads",
    "thread_messages",
    "thread_shares",
    "message_reports",
    "thread_share_recipients",
    "memory",
    "memory_proposal",
    "memory_audit",
    "user_settings",
}


@pytest.fixture
def empty_schema() -> Iterator[Engine]:
    """A schema with nothing in it, not even `create_all`'s tables."""
    schema = f"mig_{uuid.uuid4().hex[:16]}"
    admin = build_engine(TEST_DATABASE_URL)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = build_engine(TEST_DATABASE_URL)

    # Same fix as `pg_engine` in conftest: a connection pooler drops the
    # `options` startup parameter, and these tests would then run against
    # `public`. Here that is not cross-test leakage but data loss -- the
    # downgrade test would drop every real table -- hence the hard assert.
    @event.listens_for(engine, "connect")
    def _apply_search_path(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    try:
        with engine.connect() as connection:
            active = connection.execute(text("SELECT current_schema()")).scalar()
            assert active == schema, (
                f"search_path did not stick (got {active!r}); refusing to run "
                "migration tests against the default schema"
            )
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


#: Resolved from this file, not from the working directory: pytest is run both
#: from `backend/` and from the repository root, and a relative "alembic.ini"
#: silently resolves to a different place in each.
_ALEMBIC_INI = pathlib.Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic_config(connection) -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.attributes["connection"] = connection
    return config


def test_upgrade_head_from_an_empty_database_creates_every_table(
    empty_schema: Engine,
) -> None:
    with empty_schema.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    tables = set(inspect(empty_schema).get_table_names())
    assert MIGRATED_TABLES <= tables, MIGRATED_TABLES - tables


def test_migrated_schema_matches_the_models(empty_schema: Engine) -> None:
    """No drift between the chain and `target_metadata`.

    Columns only. Comparing indexes as well would fail on Alembic's `ix_`
    naming for the same index, which is noise rather than drift.
    """
    with empty_schema.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    inspector = inspect(empty_schema)
    for name, table in target_metadata.tables.items():
        migrated = {column["name"] for column in inspector.get_columns(name)}
        expected = {column.name for column in table.columns}
        assert migrated == expected, f"{name}: {expected ^ migrated}"


def test_the_chain_creates_the_invariants_the_stores_rely_on(
    empty_schema: Engine,
) -> None:
    """A sequence and a partial unique index, neither of them a column.

    The drift check above compares columns, so it cannot see either. Both are
    load-bearing: the store asks the sequence for every recency number, and the
    index is what keeps one live memory row per key when a writer forgets its
    lock.
    """
    with empty_schema.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    with empty_schema.connect() as connection:
        sequences = connection.execute(
            text(
                "SELECT sequencename FROM pg_sequences "
                "WHERE schemaname = current_schema()"
            )
        ).scalars()
        assert "threads_recency_seq" in set(sequences)

        indexes = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'memory'")
        ).scalars()
        assert "memory_live_key_idx" in set(indexes)


def test_downgrade_to_base_then_upgrade_again(empty_schema: Engine) -> None:
    with empty_schema.begin() as connection:
        config = _alembic_config(connection)
        command.upgrade(config, "head")
        command.downgrade(config, "base")

    remaining = set(inspect(empty_schema).get_table_names())
    assert not (MIGRATED_TABLES & remaining), MIGRATED_TABLES & remaining

    with empty_schema.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")

    assert MIGRATED_TABLES <= set(inspect(empty_schema).get_table_names())
