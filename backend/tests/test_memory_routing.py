"""Where an auto-extracted memory goes.

Pure policy, so these tests need no database and no provider.
"""

from datetime import UTC, datetime

from rag_ingestion.memory.routing import (
    PROMOTION_THREAD_THRESHOLD,
    normalize,
    should_promote,
)
from rag_ingestion.memory.models import (
    Memory,
    MemoryKey,
    MemoryScope,
    MemorySource,
    MemoryType,
)


def _live(
    key: str,
    value: str,
    scope: MemoryScope = MemoryScope.USER,
    thread_id: str | None = None,
) -> Memory:
    now = datetime.now(UTC)
    return Memory(
        memory_id=f"mem_{key}",
        owner_user_id="user-a",
        scope=scope,
        thread_id=thread_id,
        type=MemoryType.FACT,
        key=key,
        value=value,
        source=MemorySource.LLM_EXTRACTED,
        created_at=now,
        updated_at=now,
    )


# --- promotion ---------------------------------------------------------------


def test_one_thread_is_not_enough_to_go_global() -> None:
    assert should_promote(distinct_threads=1) is False


def test_reaching_the_threshold_promotes() -> None:
    assert should_promote(distinct_threads=PROMOTION_THREAD_THRESHOLD) is True
    assert should_promote(distinct_threads=PROMOTION_THREAD_THRESHOLD + 1) is True


def test_below_the_threshold_stays_local() -> None:
    assert should_promote(distinct_threads=PROMOTION_THREAD_THRESHOLD - 1) is False


def test_the_threshold_is_five() -> None:
    """Design 6.5 said two; five in practice. One repeat is coincidence, and a
    wrong promotion leaves a global memory the user never asked for."""
    assert PROMOTION_THREAD_THRESHOLD == 5


# --- normalisation -----------------------------------------------------------


def test_normalize_is_stable_for_equivalent_values() -> None:
    assert normalize("  Keep   it Short ") == normalize("keep it short")


def test_normalize_keeps_different_values_apart() -> None:
    assert normalize("Vietnamese") != normalize("English")


def test_normalize_is_what_makes_a_restatement_not_a_change() -> None:
    """The comparison the write path runs before spending a slot.

    It lived in the retired `route()`; the tools call it through the writer
    now, and the end-to-end proof is in `test_thread_api.py`.
    """
    stored = "Keep it short"
    for restatement in ["keep it short", "  Keep It Short  ", "Keep  it   short"]:
        assert normalize(restatement) == normalize(stored), restatement


# --- promotion evidence, against a real Postgres ------------------------------


def test_distinct_thread_count_counts_separate_threads(pg_engine) -> None:
    from rag_ingestion.memory.models import MemorySource, MemoryType
    from rag_ingestion.memory.store import PostgresMemoryStore
    from rag_ingestion.storage.pg_threads import PostgresThreadStore

    store = PostgresMemoryStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)

    def write(thread_id: str, key: str = "tone") -> None:
        store.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.THREAD,
            type=MemoryType.PREFERENCE,
            key=key,
            value="Friendly",
            source=MemorySource.LLM_EXTRACTED,
            thread_id=thread_id,
        )

    first = threads.create_thread(owner_user_id="user-a", title="One").thread_id
    second = threads.create_thread(owner_user_id="user-a", title="Two").thread_id

    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 0

    write(first)
    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 1

    # Twice in the same thread is still one thread: repetition inside one
    # conversation is not evidence that the preference is a habit.
    write(first)
    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 1

    write(second)
    count = store.distinct_thread_count(owner_user_id="user-a", key="tone")
    assert count == 2
    # Two separate threads is real evidence, just not yet enough of it: the
    # count is what this test owns, the threshold lives in `should_promote`.
    assert should_promote(distinct_threads=count) is False
    assert should_promote(distinct_threads=PROMOTION_THREAD_THRESHOLD) is True


def test_distinct_thread_count_is_owner_and_key_scoped(pg_engine) -> None:
    from rag_ingestion.memory.models import MemorySource, MemoryType
    from rag_ingestion.memory.store import PostgresMemoryStore
    from rag_ingestion.storage.pg_threads import PostgresThreadStore

    store = PostgresMemoryStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)
    mine = threads.create_thread(owner_user_id="user-a", title="Mine").thread_id
    theirs = threads.create_thread(owner_user_id="user-b", title="Theirs").thread_id

    for owner, thread_id in (("user-a", mine), ("user-b", theirs)):
        store.add_memory(
            owner_user_id=owner,
            scope=MemoryScope.THREAD,
            type=MemoryType.PREFERENCE,
            key="tone",
            value="Friendly",
            source=MemorySource.LLM_EXTRACTED,
            thread_id=thread_id,
        )

    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 1
    assert store.distinct_thread_count(owner_user_id="user-a", key="response_length") == 0


def test_a_deleted_row_stops_counting(pg_engine) -> None:
    """The owner deleting it is them saying no."""
    from rag_ingestion.memory.models import MemorySource, MemoryType
    from rag_ingestion.memory.store import PostgresMemoryStore
    from rag_ingestion.storage.pg_threads import PostgresThreadStore

    store = PostgresMemoryStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)
    written = []
    for title in ("One", "Two"):
        thread_id = threads.create_thread(owner_user_id="user-a", title=title).thread_id
        written.append(
            store.add_memory(
                owner_user_id="user-a",
                scope=MemoryScope.THREAD,
                type=MemoryType.PREFERENCE,
                key="tone",
                value="Friendly",
                source=MemorySource.LLM_EXTRACTED,
                thread_id=thread_id,
            )
        )

    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 2

    store.delete_memory(owner_user_id="user-a", memory_id=written[0].memory_id)

    assert store.distinct_thread_count(owner_user_id="user-a", key="tone") == 1
