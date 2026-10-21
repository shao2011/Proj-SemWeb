"""Who closes what the app is holding.

`create_runtime_app` either receives a resource or opens one. It registered a
shutdown handler for the executor and for a Redis client it opened, and none for
the engine or the Qdrant client it opened, so every constructed app left those
two holding connections until the process ended. Repeated construction, which
the suite does constantly, is where that accumulates.

The mirror rule matters as much: a resource passed in belongs to the caller, and
closing it on shutdown would pull it out from under them.
"""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import Engine

from rag_ingestion.api.app import RuntimeConfig, create_runtime_app
from rag_ingestion.config import ProviderConfig
from tests.conftest import TEST_DATABASE_URL


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        qdrant_url="http://unused:6333",
        database_url=TEST_DATABASE_URL,
        providers=ProviderConfig(collection_prefix="rag"),
    )


def _shutdown_owners(app) -> list[object]:
    """The objects the shutdown handlers are bound to."""
    return [
        handler.__self__
        for handler in app.router.on_shutdown
        if hasattr(handler, "__self__")
    ]


def _owner_types(app) -> set[str]:
    return {type(owner).__name__ for owner in _shutdown_owners(app)}


def test_resources_passed_in_are_left_to_their_owner(pg_engine: Engine) -> None:
    qdrant = QdrantClient(location=":memory:")

    app = create_runtime_app(_config(), qdrant_client=qdrant, engine=pg_engine)

    owners = _shutdown_owners(app)
    assert pg_engine not in owners
    assert qdrant not in owners
    # Only the executor, which the app always builds itself.
    assert _owner_types(app) == {"ThreadPoolExecutor"}
    # Still usable afterwards, which is the point of not closing them.
    with pg_engine.connect():
        pass


def test_resources_the_app_opens_are_closed_on_shutdown(pg_engine: Engine) -> None:
    """Built without an engine or a client, so the app owns both."""
    del pg_engine  # only needed so the throwaway schema exists

    app = create_runtime_app(_config(), create_tables=False)

    assert _owner_types(app) == {"ThreadPoolExecutor", "QdrantClient", "Engine"}


@pytest.mark.parametrize("create_tables", [True, False])
def test_shutdown_runs_clean(pg_engine: Engine, create_tables: bool) -> None:
    """Every registered handler has to survive being called."""
    from fastapi.testclient import TestClient

    app = create_runtime_app(
        _config(), engine=pg_engine, create_tables=create_tables
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    # Leaving the context runs shutdown. The engine was passed in, so it must
    # still work.
    with pg_engine.connect():
        pass
