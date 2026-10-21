from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, update
from sqlmodel import Session, col

from rag_ingestion.memory.models import (
    MEMORY_CAPS,
    Memory,
    MemoryKey,
    MemoryOperation,
    MemoryScope,
    MemorySource,
    MemoryStore,
    MemoryType,
    is_valid_key,
)
from rag_ingestion.db.tables import MemoryRow
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


# --- key list ---------------------------------------------------------------


def test_closed_key_list_is_english_and_covers_the_settings_form() -> None:
    keys = {item.value for item in MemoryKey}

    assert "nickname" in keys
    assert "occupation" in keys
    assert {"response_language", "response_length", "response_format", "tone"} <= keys
    assert all(key.isascii() and key.islower() for key in keys)


@pytest.mark.parametrize(
    "key",
    ["nickname", "occupation", "other:client-name", "other:a_b-1"],
)
def test_valid_keys_are_accepted(key: str) -> None:
    assert is_valid_key(key)


@pytest.mark.parametrize(
    "key",
    ["job", "Nickname", "other:", "other:UPPER", "other:has space", "", "nghe_nghiep"],
)
def test_invalid_keys_are_rejected(key: str) -> None:
    assert not is_valid_key(key)


def test_memory_rejects_a_key_outside_the_closed_list(store: PostgresMemoryStore) -> None:
    with pytest.raises(ValidationError):
        _add(store, key="job")


# --- scope integrity --------------------------------------------------------


def test_store_satisfies_the_protocol(store: PostgresMemoryStore) -> None:
    assert isinstance(store, MemoryStore)


def test_thread_scope_requires_a_thread_id(store: PostgresMemoryStore) -> None:
    with pytest.raises(ValidationError):
        _add(store, scope=MemoryScope.THREAD, thread_id=None)


def test_user_scope_must_not_target_a_thread(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    with pytest.raises(ValidationError):
        _add(store, scope=MemoryScope.USER, thread_id=_thread(pg_engine))


def test_project_scope_requires_a_project_id(store: PostgresMemoryStore) -> None:
    with pytest.raises(ValidationError):
        _add(store, scope=MemoryScope.PROJECT, project_id=None)


# --- ownership --------------------------------------------------------------


def test_memory_is_private_to_its_owner(store: PostgresMemoryStore) -> None:
    memory = _add(store)

    assert store.get_memory(owner_user_id="user-b", memory_id=memory.memory_id) is None
    assert store.list_memories(owner_user_id="user-b") == []
    assert (
        store.update_memory(
            owner_user_id="user-b", memory_id=memory.memory_id, value="stolen"
        )
        is None
    )
    assert (
        store.delete_memory(owner_user_id="user-b", memory_id=memory.memory_id) is False
    )


# --- four operations --------------------------------------------------------


def test_add_writes_an_audit_row(store: PostgresMemoryStore) -> None:
    memory = _add(store)

    trail = store.audit_trail(memory_id=memory.memory_id)

    assert [entry.op for entry in trail] == ["ADD"]
    assert trail[0].source == MemorySource.USER_EXPLICIT


def test_adding_the_same_key_supersedes_the_previous_value(
    store: PostgresMemoryStore,
) -> None:
    first = _add(store, value="Keep answers short.")
    second = _add(store, value="Answer in full detail.")

    live = store.list_memories(owner_user_id="user-a")
    assert [item.memory_id for item in live] == [second.memory_id]

    superseded = store.get_memory(
        owner_user_id="user-a", memory_id=first.memory_id, include_deleted=True
    )
    assert superseded is not None
    assert superseded.superseded_by == second.memory_id
    assert superseded.deleted_at is not None


def test_supersede_is_scoped_so_a_thread_memory_does_not_replace_a_user_one(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    user_memory = _add(store, value="Global short answers.")
    thread_id = _thread(pg_engine)
    thread_memory = _add(
        store,
        scope=MemoryScope.THREAD,
        thread_id=thread_id,
        value="Long answers in this thread.",
    )

    ids = {item.memory_id for item in store.list_memories(owner_user_id="user-a")}
    assert ids == {user_memory.memory_id, thread_memory.memory_id}


def test_update_changes_the_value_and_logs_it(store: PostgresMemoryStore) -> None:
    memory = _add(store)

    updated = store.update_memory(
        owner_user_id="user-a", memory_id=memory.memory_id, value="Be concise."
    )

    assert updated is not None
    assert updated.value == "Be concise."
    assert updated.updated_at >= memory.updated_at
    assert [entry.op for entry in store.audit_trail(memory_id=memory.memory_id)] == [
        "ADD",
        "UPDATE",
    ]


def test_update_can_promote_a_thread_memory_to_user_scope(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    memory = _add(
        store, scope=MemoryScope.THREAD, thread_id=_thread(pg_engine), value="Short."
    )

    promoted = store.update_memory(
        owner_user_id="user-a", memory_id=memory.memory_id, promote_to_user=True
    )

    assert promoted is not None
    assert promoted.scope is MemoryScope.USER
    assert promoted.thread_id is None


def test_delete_marks_rather_than_removing(store: PostgresMemoryStore) -> None:
    memory = _add(store)

    assert store.delete_memory(owner_user_id="user-a", memory_id=memory.memory_id) is True
    assert store.list_memories(owner_user_id="user-a") == []
    assert store.get_memory(owner_user_id="user-a", memory_id=memory.memory_id) is None

    marked = store.get_memory(
        owner_user_id="user-a", memory_id=memory.memory_id, include_deleted=True
    )
    assert marked is not None
    assert marked.deleted_at is not None
    assert [entry.op for entry in store.audit_trail(memory_id=memory.memory_id)] == [
        "ADD",
        "DELETE",
    ]
    assert store.delete_memory(owner_user_id="user-a", memory_id=memory.memory_id) is False


# --- caps -------------------------------------------------------------------



def test_a_scope_refuses_a_write_past_its_ceiling(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """The ceiling is a runaway-writer guard, not a working limit, so this
    builds its own small one rather than filling five hundred rows."""
    small = PostgresMemoryStore(pg_engine, caps={scope: 2 for scope in MemoryScope})
    thread_id = _thread(pg_engine)
    for index in range(2):
        small.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.THREAD,
            type=MemoryType.FACT,
            key=f"other:note-{index}",
            value=f"Note {index}",
            source=MemorySource.USER_EXPLICIT,
            thread_id=thread_id,
        )

    with pytest.raises(ValueError, match="cap"):
        small.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.THREAD,
            type=MemoryType.FACT,
            key="other:overflow",
            value="Overflow",
            source=MemorySource.USER_EXPLICIT,
            thread_id=thread_id,
        )


def test_deleted_memories_free_cap_space(store: PostgresMemoryStore) -> None:
    created = [
        _add(store, key=f"other:fact-{index}", value=f"Fact {index}")
        for index in range(_TEST_CAP)
    ]
    store.delete_memory(owner_user_id="user-a", memory_id=created[0].memory_id)

    assert _add(store, key="other:room-now", value="Fits").memory_id


# --- cascade ----------------------------------------------------------------


def test_thread_memory_dies_with_its_thread(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    threads = PostgresThreadStore(pg_engine)
    thread = threads.create_thread(owner_user_id="user-a", title="Chat")
    _add(store, scope=MemoryScope.THREAD, thread_id=thread.thread_id, value="Short.")

    threads.delete_thread(owner_user_id="user-a", thread_id=thread.thread_id)

    assert store.list_memories(owner_user_id="user-a") == []


# --- proposals --------------------------------------------------------------


def test_confirming_a_proposal_writes_a_user_confirmed_memory(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    thread_id = _thread(pg_engine)
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=thread_id,
        suggested_scope=MemoryScope.USER,
        type=MemoryType.PREFERENCE,
        key=MemoryKey.TONE,
        value="Friendly tone.",
    )

    assert [item.proposal_id for item in store.list_proposals(owner_user_id="user-a")] == [
        proposal.proposal_id
    ]

    written = store.confirm_proposal(
        owner_user_id="user-a", proposal_id=proposal.proposal_id
    )

    assert written is not None
    assert written.source is MemorySource.USER_CONFIRMED
    assert written.scope is MemoryScope.USER
    assert written.value == "Friendly tone."
    assert store.list_proposals(owner_user_id="user-a") == []


def test_proposals_are_owner_scoped(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=_thread(pg_engine),
        suggested_scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="other:client",
        value="Client is ACME.",
    )

    assert store.list_proposals(owner_user_id="user-b") == []
    assert (
        store.confirm_proposal(
            owner_user_id="user-b", proposal_id=proposal.proposal_id
        )
        is None
    )
    assert (
        store.dismiss_proposal(
            owner_user_id="user-b", proposal_id=proposal.proposal_id
        )
        is False
    )


def test_dismissing_a_proposal_writes_no_memory(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    proposal = store.add_proposal(
        owner_user_id="user-a",
        thread_id=_thread(pg_engine),
        suggested_scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="other:client",
        value="Client is ACME.",
    )

    assert (
        store.dismiss_proposal(
            owner_user_id="user-a", proposal_id=proposal.proposal_id
        )
        is True
    )
    assert store.list_proposals(owner_user_id="user-a") == []
    assert store.list_memories(owner_user_id="user-a") == []


# --- settings ---------------------------------------------------------------


def test_settings_default_to_memory_enabled(store: PostgresMemoryStore) -> None:
    settings = store.get_settings(owner_user_id="user-a")

    assert settings.owner_user_id == "user-a"
    assert settings.memory_enabled is True


def test_memory_can_be_disabled_and_re_enabled(store: PostgresMemoryStore) -> None:
    store.set_memory_enabled(owner_user_id="user-a", enabled=False)
    assert store.get_settings(owner_user_id="user-a").memory_enabled is False

    store.set_memory_enabled(owner_user_id="user-a", enabled=True)
    assert store.get_settings(owner_user_id="user-a").memory_enabled is True


def test_settings_are_per_user(store: PostgresMemoryStore) -> None:
    store.set_memory_enabled(owner_user_id="user-a", enabled=False)

    assert store.get_settings(owner_user_id="user-b").memory_enabled is True


# --- filtering --------------------------------------------------------------


# --- recall -----------------------------------------------------------------


def test_recall_unions_the_scopes_rather_than_filtering(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    """The read path needs user-scoped rows alongside the thread's own.

    `list_memories(thread_id=...)` filters on the column, and user-scoped rows
    carry a NULL `thread_id`, so reusing it here dropped every global memory.
    """
    thread_id = _thread(pg_engine)
    _add(store, value="Global.")
    _add(
        store,
        scope=MemoryScope.THREAD,
        thread_id=thread_id,
        key=MemoryKey.TONE,
        value="Local.",
    )

    values = {
        item.value
        for item in store.recall(owner_user_id="user-a", thread_id=thread_id)
    }

    assert values == {"Global.", "Local."}
    assert [
        item.value for item in store.list_memories(owner_user_id="user-a", thread_id=thread_id)
    ] == ["Local."]


def test_recall_ignores_another_threads_memory(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    mine = _thread(pg_engine)
    theirs = _thread(pg_engine)
    _add(store, scope=MemoryScope.THREAD, thread_id=theirs, value="Their note.")

    assert store.recall(owner_user_id="user-a", thread_id=mine) == []


def test_recall_without_a_thread_returns_only_user_scope(
    store: PostgresMemoryStore, pg_engine: Engine
) -> None:
    _add(store, value="Global.")
    _add(
        store,
        scope=MemoryScope.THREAD,
        thread_id=_thread(pg_engine),
        key=MemoryKey.TONE,
        value="Local.",
    )

    assert [item.value for item in store.recall(owner_user_id="user-a")] == ["Global."]


def test_recall_skips_deleted_and_superseded(store: PostgresMemoryStore) -> None:
    first = _add(store, value="Old.")
    _add(store, value="New.")
    dropped = _add(store, key=MemoryKey.TONE, value="Doomed.")
    store.delete_memory(owner_user_id="user-a", memory_id=dropped.memory_id)

    values = [item.value for item in store.recall(owner_user_id="user-a")]

    assert values == ["New."]
    assert first.memory_id not in {item.memory_id for item in store.recall(owner_user_id="user-a")}


def test_recall_is_owner_scoped(store: PostgresMemoryStore) -> None:
    _add(store, value="Mine.")

    assert store.recall(owner_user_id="user-b") == []


def test_listing_filters_by_scope(store: PostgresMemoryStore, pg_engine: Engine) -> None:
    _add(store, value="Global.")
    thread_id = _thread(pg_engine)
    _add(store, scope=MemoryScope.THREAD, thread_id=thread_id, value="Local.")

    user_only = store.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    thread_only = store.list_memories(
        owner_user_id="user-a", scope=MemoryScope.THREAD, thread_id=thread_id
    )

    assert [item.value for item in user_only] == ["Global."]
    assert [item.value for item in thread_only] == ["Local."]


# --- last_used_at and LRU eviction --------------------------------------------


#: Eviction only shows up at a full scope, and the real ceiling is 500. These
#: tests build a store with a tiny one so they exercise the policy, not the
#: number.
_TEST_CAP = 3


def _small_store(pg_engine) -> PostgresMemoryStore:
    return PostgresMemoryStore(pg_engine, caps={scope: _TEST_CAP for scope in MemoryScope})


def _fill_user_scope(store, owner: str, count: int, source=MemorySource.LLM_EXTRACTED):
    written = []
    for index in range(count):
        written.append(
            store.add_memory(
                owner_user_id=owner,
                scope=MemoryScope.USER,
                type=MemoryType.FACT,
                key=f"other:filler-{index}",
                value=f"Filler {index}",
                source=source,
            )
        )
    return written


def test_a_full_scope_evicts_the_least_recently_used_instead_of_refusing(
    pg_engine,
) -> None:
    """The cap is a budget, not a wall: an automatic pipeline that cannot write
    is a pipeline that silently forgets whatever the user just said."""
    store = _small_store(pg_engine)
    owner = "user-lru"
    written = _fill_user_scope(store, owner, _TEST_CAP)

    fresh = store.add_memory(
        owner_user_id=owner,
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="nickname",
        value="Dat",
        source=MemorySource.LLM_EXTRACTED,
    )

    live = store.list_memories(owner_user_id=owner, scope=MemoryScope.USER)
    keys = {item.key for item in live}
    assert len(live) == _TEST_CAP
    assert fresh.key in keys
    assert written[0].key not in keys, "the oldest row makes way"
    assert written[-1].key in keys


def test_eviction_is_recorded_in_the_audit_trail(pg_engine) -> None:
    store = _small_store(pg_engine)
    owner = "user-lru-audit"
    written = _fill_user_scope(store, owner, _TEST_CAP)

    store.add_memory(
        owner_user_id=owner,
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="nickname",
        value="Dat",
        source=MemorySource.LLM_EXTRACTED,
    )

    evicted = next(
        item
        for item in written
        if store.get_memory(
            owner_user_id=owner, memory_id=item.memory_id, include_deleted=True
        ).deleted_at
        is not None
    )
    assert [entry.op for entry in store.audit_trail(memory_id=evicted.memory_id)] == [
        MemoryOperation.ADD,
        MemoryOperation.DELETE,
    ]


def test_memory_the_user_wrote_themselves_is_never_evicted(pg_engine) -> None:
    """Eviction is the cost of an automatic writer being wrong. What the user
    typed is not the pipeline's to throw away."""
    store = _small_store(pg_engine)
    owner = "user-explicit"
    _fill_user_scope(
        store, owner, _TEST_CAP, source=MemorySource.USER_EXPLICIT
    )

    with pytest.raises(ValueError, match="cap"):
        store.add_memory(
            owner_user_id=owner,
            scope=MemoryScope.USER,
            type=MemoryType.FACT,
            key="nickname",
            value="Dat",
            source=MemorySource.LLM_EXTRACTED,
        )

    assert (
        len(store.list_memories(owner_user_id=owner, scope=MemoryScope.USER))
        == _TEST_CAP
    )


def test_eviction_stays_inside_one_scope(pg_engine) -> None:
    store = _small_store(pg_engine)
    owner = "user-scope-fence"
    threads = PostgresThreadStore(pg_engine)
    thread_id = threads.create_thread(owner_user_id=owner, title="T").thread_id
    for index in range(_TEST_CAP):
        store.add_memory(
            owner_user_id=owner,
            scope=MemoryScope.THREAD,
            type=MemoryType.FACT,
            key=f"other:thread-{index}",
            value=str(index),
            source=MemorySource.LLM_EXTRACTED,
            thread_id=thread_id,
        )
    (global_memory,) = _fill_user_scope(store, owner, 1)

    store.add_memory(
        owner_user_id=owner,
        scope=MemoryScope.THREAD,
        type=MemoryType.FACT,
        key="nickname",
        value="Dat",
        source=MemorySource.LLM_EXTRACTED,
        thread_id=thread_id,
    )

    assert [item.key for item in store.list_memories(owner_user_id=owner, scope=MemoryScope.USER)] == [
        global_memory.key
    ]


def test_a_memory_created_moments_ago_is_not_the_first_evicted(pg_engine) -> None:
    """Creation counts as the memory having mattered.

    Grouping "never used" ahead of everything else meant a memory saved one
    second ago was thrown away before one last read a year earlier.
    """
    store = _small_store(pg_engine)
    owner = "user-fresh"
    written = _fill_user_scope(store, owner, _TEST_CAP - 1)
    # Every existing row was read long ago; the newest row has never been read.
    long_ago = datetime.now(UTC) - timedelta(days=365)
    with Session(pg_engine) as session:
        session.exec(
            update(MemoryRow)
            .where(col(MemoryRow.owner_user_id) == owner)
            .values(last_used_at=long_ago, created_at=long_ago)
        )
        session.commit()
    just_saved = store.add_memory(
        owner_user_id=owner,
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="nickname",
        value="Dat",
        source=MemorySource.LLM_EXTRACTED,
    )

    store.add_memory(
        owner_user_id=owner,
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="occupation",
        value="Accountant",
        source=MemorySource.LLM_EXTRACTED,
    )

    live = {item.key for item in store.list_memories(owner_user_id=owner, scope=MemoryScope.USER)}
    assert just_saved.key in live, "the newest memory survives its first turn"
    assert written[0].key not in live


def test_the_store_protocol_matches_what_callers_actually_pass(pg_engine) -> None:
    """A Protocol that omits a parameter every caller passes is not a contract.

    `source_message_id` is what ties an audit entry back to the message that
    caused the write; a second implementation written against the Protocol
    would have failed on the first call.
    """
    import inspect

    from rag_ingestion.memory.models import MemoryStore

    declared = inspect.signature(MemoryStore.add_memory).parameters
    implemented = inspect.signature(PostgresMemoryStore.add_memory).parameters

    assert set(implemented) - set(declared) == set()
