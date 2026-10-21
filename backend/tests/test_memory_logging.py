"""Observability of memory writes.

Every path that changes stored memory logs one line. The store is the place for
it rather than the routes: extraction, the settings UI, and proposal confirm all
reach the database through here, so one set of log calls covers all three.

Values are deliberately absent from the log. The value is the user's own
personal data, which is the one field that must not end up in a log file, so
each test asserts the message carries identifiers and not content.
"""

import logging

import pytest
from sqlalchemy import Engine

from rag_ingestion.memory.models import MemoryScope, MemorySource, MemoryType
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore

_LOGGER = "rag_ingestion.memory.store"

SECRET = "Wxrdie-do-not-log-this"


@pytest.fixture
def store(pg_engine: Engine) -> PostgresMemoryStore:
    return PostgresMemoryStore(pg_engine)


def _thread(pg_engine: Engine, owner: str = "user-a") -> str:
    return (
        PostgresThreadStore(pg_engine)
        .create_thread(owner_user_id=owner, title="Chat")
        .thread_id
    )


def _add(store: PostgresMemoryStore, **overrides) -> object:
    kwargs = {
        "owner_user_id": "user-a",
        "scope": MemoryScope.USER,
        "type": MemoryType.FACT,
        "key": "nickname",
        "value": SECRET,
        "source": MemorySource.LLM_EXTRACTED,
    }
    kwargs.update(overrides)
    return store.add_memory(**kwargs)


def test_add_logs_the_write(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        memory = _add(store)

    assert "ADD" in caplog.text
    assert memory.memory_id in caplog.text
    assert "nickname" in caplog.text
    assert "user" in caplog.text
    assert "llm_extracted" in caplog.text


def test_supersede_logs_the_row_it_replaced(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The rewrite case: the log has to name the row that was replaced.

    Without the old id the trail cannot be followed backwards, which is the
    whole reason supersede is preferred to an in-place overwrite.
    """
    first = _add(store, value="Old name")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        second = _add(store, value="New name")

    assert first.memory_id in caplog.text
    assert second.memory_id in caplog.text
    assert "superseded" in caplog.text


def test_add_without_a_previous_key_does_not_claim_a_supersede(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _add(store, key="occupation")

    assert "superseded" not in caplog.text


def test_update_logs_the_change(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    memory = _add(store)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        store.update_memory(
            owner_user_id="user-a", memory_id=memory.memory_id, value="Edited"
        )

    assert "UPDATE" in caplog.text
    assert memory.memory_id in caplog.text


def test_promotion_is_visible_in_the_update_log(
    pg_engine: Engine, store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    thread_id = _thread(pg_engine)
    memory = _add(store, scope=MemoryScope.THREAD, thread_id=thread_id)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        store.update_memory(
            owner_user_id="user-a", memory_id=memory.memory_id, promote_to_user=True
        )

    assert "promoted" in caplog.text


def test_delete_logs_the_mark(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    memory = _add(store)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        store.delete_memory(owner_user_id="user-a", memory_id=memory.memory_id)

    assert "DELETE" in caplog.text
    assert memory.memory_id in caplog.text


def test_a_miss_logs_nothing(
    store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing changed, so there is nothing to report."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        store.update_memory(owner_user_id="user-a", memory_id="mem_missing", value="x")
        store.delete_memory(owner_user_id="user-a", memory_id="mem_missing")

    assert caplog.text == ""


def test_proposal_is_logged(
    pg_engine: Engine, store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A user-scoped extraction lands here, not in memory.

    Without this line the log says "extracted 1 candidate" and the memory table
    stays empty, with nothing explaining where the candidate went.
    """
    thread_id = _thread(pg_engine)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        proposal = store.add_proposal(
            owner_user_id="user-a",
            thread_id=thread_id,
            suggested_scope=MemoryScope.USER,
            type=MemoryType.FACT,
            key="nickname",
            value=SECRET,
        )

    assert "PROPOSE" in caplog.text
    assert proposal.proposal_id in caplog.text
    assert "nickname" in caplog.text


def test_no_write_path_logs_the_value(
    pg_engine: Engine, store: PostgresMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    thread_id = _thread(pg_engine)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        memory = _add(store)
        _add(store, value=SECRET + "-again")
        store.update_memory(
            owner_user_id="user-a", memory_id=memory.memory_id, value=SECRET
        )
        store.delete_memory(owner_user_id="user-a", memory_id=memory.memory_id)
        store.add_proposal(
            owner_user_id="user-a",
            thread_id=thread_id,
            suggested_scope=MemoryScope.USER,
            type=MemoryType.FACT,
            key="occupation",
            value=SECRET,
        )

    assert "Wxrdie" not in caplog.text
