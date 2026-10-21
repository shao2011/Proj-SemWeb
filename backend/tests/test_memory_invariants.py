"""Memory write invariants: caps, supersede, promotion, confirm.

Separate from `test_memory_store.py`, which covers the happy paths. These are
the rules the store has to hold even when two writers arrive at once or a write
is rejected halfway through, and each one here failed before.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from rag_ingestion.db.tables import MemoryRow
from rag_ingestion.memory.models import (
    MEMORY_CAPS,
    Memory,
    MemoryKey,
    MemoryScope,
    MemorySource,
    MemoryType,
)
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore


@pytest.fixture
def store(pg_engine: Engine) -> PostgresMemoryStore:
    return PostgresMemoryStore(pg_engine)


def _thread(pg_engine: Engine, owner: str = "user-a") -> str:
    return (
        PostgresThreadStore(pg_engine)
        .create_thread(owner_user_id=owner, title="Chat")
        .thread_id
    )


def _add(store: PostgresMemoryStore, **overrides) -> Memory:
    payload = {
        "owner_user_id": "user-a",
        "scope": MemoryScope.USER,
        "type": MemoryType.PREFERENCE,
        "key": MemoryKey.RESPONSE_LENGTH,
        "value": "Keep answers short.",
        "source": MemorySource.USER_EXPLICIT,
    }
    payload.update(overrides)
    return store.add_memory(**payload)


# --- promotion --------------------------------------------------------------


def test_promotion_supersedes_the_existing_user_memory_on_the_same_key(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """Promotion has to go through the same rules as a write.

    It only reassigned `scope` before, so the older global value stayed live
    beside the promoted one and which of the two won a recall came down to row
    order rather than to supersede.
    """
    older = _add(store, value="Answer at length.")
    local = _add(
        store,
        scope=MemoryScope.THREAD,
        thread_id=_thread(pg_engine),
        value="Keep answers short.",
    )

    promoted = store.update_memory(
        owner_user_id="user-a", memory_id=local.memory_id, promote_to_user=True
    )

    assert promoted is not None
    assert promoted.scope is MemoryScope.USER
    assert promoted.thread_id is None
    live = store.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    assert [item.memory_id for item in live] == [promoted.memory_id]
    assert store.get_memory(owner_user_id="user-a", memory_id=older.memory_id) is None
    superseded = store.get_memory(
        owner_user_id="user-a", memory_id=older.memory_id, include_deleted=True
    )
    assert superseded is not None
    assert superseded.superseded_by == promoted.memory_id


def test_promotion_refuses_to_exceed_the_user_cap(pg_engine: Engine) -> None:
    # Its own ceiling: the shipped one is 500, and what this pins is that
    # promotion goes through the cap check, not what the number is.
    store = PostgresMemoryStore(pg_engine, caps={scope: 2 for scope in MemoryScope})
    for number in range(2):
        _add(store, key=f"other:filler{number}")
    local = _add(
        store,
        scope=MemoryScope.THREAD,
        thread_id=_thread(pg_engine),
        key="other:newcomer",
    )

    with pytest.raises(ValueError, match="cap"):
        store.update_memory(
            owner_user_id="user-a", memory_id=local.memory_id, promote_to_user=True
        )

    unchanged = store.get_memory(owner_user_id="user-a", memory_id=local.memory_id)
    assert unchanged is not None
    assert unchanged.scope is MemoryScope.THREAD


def test_promotion_of_a_project_memory_clears_its_project(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """A user-scoped row must target neither a thread nor a project."""
    thread_id = _thread(pg_engine)
    local = _add(store, scope=MemoryScope.THREAD, thread_id=thread_id)

    promoted = store.update_memory(
        owner_user_id="user-a",
        memory_id=local.memory_id,
        value="Now global.",
        promote_to_user=True,
    )

    assert promoted is not None
    assert promoted.thread_id is None
    assert promoted.project_id is None
    assert promoted.value == "Now global."


def test_promotion_writes_one_audit_entry(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    local = _add(store, scope=MemoryScope.THREAD, thread_id=_thread(pg_engine))

    store.update_memory(
        owner_user_id="user-a", memory_id=local.memory_id, promote_to_user=True
    )

    trail = store.audit_trail(memory_id=local.memory_id)
    assert [entry.op.value for entry in trail] == ["ADD", "UPDATE"]


# --- confirm ----------------------------------------------------------------


def test_confirming_the_same_proposal_twice_writes_one_memory(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """Confirm has to be idempotent.

    The write and the dismiss used to sit in separate transactions, so a repeat
    could read a proposal the first call had already acted on and write a second
    memory for it.
    """
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=_thread(pg_engine),
        suggested_scope=MemoryScope.USER,
        type=MemoryType.PREFERENCE,
        key=MemoryKey.TONE,
        value="Friendly tone.",
    )

    first = store.confirm_proposal(
        owner_user_id="user-a", proposal_id=proposal.proposal_id
    )
    second = store.confirm_proposal(
        owner_user_id="user-a", proposal_id=proposal.proposal_id
    )

    assert first is not None
    assert second is None
    assert len(store.list_memories(owner_user_id="user-a")) == 1


def test_a_rejected_confirm_leaves_the_proposal_in_place(pg_engine: Engine) -> None:
    """One transaction, so a rejected write must not consume the proposal.

    The cap is the reachable way in: extraction proposes exactly because the
    scope was full, and it can still be full when the user gets round to
    confirming.
    """
    store = PostgresMemoryStore(pg_engine, caps={scope: 2 for scope in MemoryScope})
    for number in range(2):
        _add(store, key=f"other:filler{number}")
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=_thread(pg_engine),
        suggested_scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="other:newcomer",
        value="Still worth keeping.",
    )

    with pytest.raises(ValueError, match="cap"):
        store.confirm_proposal(
            owner_user_id="user-a", proposal_id=proposal.proposal_id
        )

    assert [
        item.proposal_id for item in store.list_proposals(owner_user_id="user-a")
    ] == [proposal.proposal_id]


def test_confirming_a_thread_proposal_keeps_its_thread(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    thread_id = _thread(pg_engine)
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=thread_id,
        suggested_scope=MemoryScope.THREAD,
        type=MemoryType.PREFERENCE,
        key=MemoryKey.TONE,
        value="Formal here.",
    )

    written = store.confirm_proposal(
        owner_user_id="user-a", proposal_id=proposal.proposal_id
    )

    assert written is not None
    assert written.scope is MemoryScope.THREAD
    assert written.thread_id == thread_id


# --- concurrency ------------------------------------------------------------


def test_the_database_refuses_a_second_live_row_on_one_key(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """The invariant belongs to the schema, not only to the supersede pass.

    Written through the row directly, which is the shape a lost supersede takes:
    the store's own path can no longer produce it.
    """
    existing = _add(store)
    now = datetime.now(UTC)

    with Session(pg_engine) as session:
        session.add(
            MemoryRow(
                memory_id="mem_duplicate",
                owner_user_id=existing.owner_user_id,
                scope=existing.scope.value,
                thread_id=None,
                project_id=None,
                type=existing.type.value,
                key=existing.key,
                value="Contradicts the live one.",
                source=existing.source.value,
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_deleted_row_does_not_block_the_key(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """The unique index is partial for a reason: history repeats keys."""
    first = _add(store, value="One.")
    store.delete_memory(owner_user_id="user-a", memory_id=first.memory_id)

    second = _add(store, value="Two.")

    assert second.memory_id != first.memory_id
    assert [
        item.value
        for item in store.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    ] == ["Two."]


def test_a_write_waits_for_the_scope_lock(pg_engine: Engine) -> None:
    """The serialization itself, asserted rather than raced for.

    Two threads left to collide may simply not overlap, so the lock is tested
    by holding it: with it held elsewhere, a write into that scope must not get
    through, and it must get through once the holder lets go. Reaching for
    `_scope_lock_key` is deliberate, the lock id has no public surface and the
    test has to name the same scope the store will.
    """
    store = PostgresMemoryStore(pg_engine)
    now = datetime.now(UTC)
    probe = Memory(
        memory_id="mem_probe",
        owner_user_id="user-a",
        scope=MemoryScope.USER,
        type=MemoryType.PREFERENCE,
        key=MemoryKey.TONE,
        value="Probe.",
        source=MemorySource.USER_EXPLICIT,
        created_at=now,
        updated_at=now,
    )
    finished = threading.Event()

    def write() -> None:
        store.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.USER,
            type=MemoryType.PREFERENCE,
            key=MemoryKey.TONE,
            value="Written once the lock is free.",
            source=MemorySource.USER_EXPLICIT,
        )
        finished.set()

    worker = threading.Thread(target=write)
    with Session(pg_engine) as holder:
        holder.exec(select(func.pg_advisory_xact_lock(store._scope_lock_key(probe))))
        worker.start()
        assert not finished.wait(timeout=2.0), "the write ignored the scope lock"
    # Leaving the session rolls its transaction back, which is what releases the
    # lock: `pg_advisory_xact_lock` is held for the transaction, not the session.

    worker.join(timeout=30)
    assert finished.is_set()
    assert len(store.list_memories(owner_user_id="user-a")) == 1


def test_concurrent_writes_on_one_key_leave_one_live_row(pg_engine: Engine) -> None:
    """Two writers, same key, same scope, at the same time.

    `add_memory` read the live rows, decided, then inserted, with nothing
    serializing the three steps, so both could insert and neither supersede.
    Asserted on the invariant rather than on an interleaving: however the two
    are scheduled, one live row for the key is the only acceptable end state.
    """
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def write(value: str) -> None:
        store = PostgresMemoryStore(pg_engine)
        try:
            barrier.wait(timeout=10)
            store.add_memory(
                owner_user_id="user-a",
                scope=MemoryScope.USER,
                type=MemoryType.PREFERENCE,
                key=MemoryKey.RESPONSE_LENGTH,
                value=value,
                source=MemorySource.USER_EXPLICIT,
            )
        except BaseException as error:  # noqa: BLE001 - reported, not swallowed
            failures.append(error)

    workers = [
        threading.Thread(target=write, args=(f"Value {number}.",)) for number in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert not failures, failures
    live = PostgresMemoryStore(pg_engine).list_memories(
        owner_user_id="user-a", scope=MemoryScope.USER
    )
    assert len(live) == 1


def test_concurrent_writes_cannot_both_pass_a_full_cap(pg_engine: Engine) -> None:
    """One slot left, two writers with different keys.

    The cap is an invariant, so exactly one may take the slot and the other has
    to be told the scope is full.
    """
    # A ceiling of its own: the shipped one is 500, and this test is about the
    # lock, not the number.
    caps = {scope: 3 for scope in MemoryScope}
    seed = PostgresMemoryStore(pg_engine, caps=caps)
    for number in range(caps[MemoryScope.USER] - 1):
        _add(seed, key=f"other:filler{number}")

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def write(key: str) -> None:
        store = PostgresMemoryStore(pg_engine, caps=caps)
        try:
            barrier.wait(timeout=10)
            store.add_memory(
                owner_user_id="user-a",
                scope=MemoryScope.USER,
                type=MemoryType.FACT,
                key=key,
                value="Contended.",
                source=MemorySource.USER_EXPLICIT,
            )
            result = "written"
        except ValueError:
            result = "capped"
        with lock:
            outcomes.append(result)

    workers = [
        threading.Thread(target=write, args=(f"other:contender{number}",))
        for number in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert sorted(outcomes) == ["capped", "written"]
    live = PostgresMemoryStore(pg_engine).list_memories(
        owner_user_id="user-a", scope=MemoryScope.USER, limit=200
    )
    assert len(live) == caps[MemoryScope.USER]
