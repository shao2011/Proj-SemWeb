import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlmodel import Session

from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.threads import Citation, ThreadMessageRole, ThreadStore


@pytest.fixture
def store(thread_store: PostgresThreadStore) -> PostgresThreadStore:
    return thread_store


def test_store_satisfies_the_protocol(store: PostgresThreadStore) -> None:
    assert isinstance(store, ThreadStore)


def test_thread_without_a_project_is_a_plain_chat(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Everyday chat")

    assert thread.project_id is None
    assert thread.thread_id.startswith("thr_")
    assert thread.created_at == thread.updated_at


def test_thread_can_be_pinned_to_a_project(store: PostgresThreadStore) -> None:
    thread = store.create_thread(
        owner_user_id="user-a",
        title="Contract review",
        project_id="prj_abc",
    )

    assert thread.project_id == "prj_abc"
    assert store.get_thread(
        owner_user_id="user-a", thread_id=thread.thread_id
    ) == thread


def test_threads_are_private_to_their_owner(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Private")

    assert store.get_thread(owner_user_id="user-b", thread_id=thread.thread_id) is None
    assert store.list_threads(owner_user_id="user-b") == []
    assert store.rename_thread(
        owner_user_id="user-b", thread_id=thread.thread_id, title="Stolen"
    ) is None
    assert store.delete_thread(owner_user_id="user-b", thread_id=thread.thread_id) is False
    assert store.add_message(
        owner_user_id="user-b",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="Injected",
    ) is None
    assert store.list_messages(owner_user_id="user-b", thread_id=thread.thread_id) is None


def test_threads_are_listed_most_recently_updated_first(store: PostgresThreadStore) -> None:
    first = store.create_thread(owner_user_id="user-a", title="First")
    second = store.create_thread(owner_user_id="user-a", title="Second")
    store.add_message(
        owner_user_id="user-a",
        thread_id=first.thread_id,
        role=ThreadMessageRole.USER,
        content="Bumps the first thread",
    )

    listed = store.list_threads(owner_user_id="user-a")

    assert [item.thread_id for item in listed] == [first.thread_id, second.thread_id]


def test_thread_listing_respects_the_limit(store: PostgresThreadStore) -> None:
    for index in range(5):
        store.create_thread(owner_user_id="user-a", title=f"Thread {index}")

    assert len(store.list_threads(owner_user_id="user-a", limit=2)) == 2


def test_rename_changes_the_title_and_keeps_the_project(store: PostgresThreadStore) -> None:
    thread = store.create_thread(
        owner_user_id="user-a", title="Old name", project_id="prj_abc"
    )

    renamed = store.rename_thread(
        owner_user_id="user-a", thread_id=thread.thread_id, title="New name"
    )

    assert renamed is not None
    assert renamed.title == "New name"
    assert renamed.project_id == "prj_abc"
    assert renamed.updated_at >= thread.updated_at


def test_renaming_does_not_move_a_thread_up_the_recents_list(
    store: PostgresThreadStore,
) -> None:
    older = store.create_thread(owner_user_id="user-a", title="Older")
    newer = store.create_thread(owner_user_id="user-a", title="Newer")

    store.rename_thread(
        owner_user_id="user-a", thread_id=older.thread_id, title="Older renamed"
    )

    listed = store.list_threads(owner_user_id="user-a")
    assert [item.thread_id for item in listed] == [newer.thread_id, older.thread_id]


def test_messages_round_trip_in_conversation_order(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Chat")
    store.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="What is article 5?",
    )
    store.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.ASSISTANT,
        content="Article 5 defines the scope.",
        citations=[
            Citation(chunk_id="chunk-1", doc_id="doc-1", block_ids=["blk-a", "blk-b"]),
            Citation(chunk_id="chunk-2", doc_id="doc-1", block_ids=["blk-c"]),
        ],
    )

    messages = store.list_messages(owner_user_id="user-a", thread_id=thread.thread_id)

    assert messages is not None
    assert [item.role for item in messages] == [
        ThreadMessageRole.USER,
        ThreadMessageRole.ASSISTANT,
    ]
    assert messages[0].citations == []
    assert messages[1].citations == [
        Citation(chunk_id="chunk-1", doc_id="doc-1", block_ids=["blk-a", "blk-b"]),
        Citation(chunk_id="chunk-2", doc_id="doc-1", block_ids=["blk-c"]),
    ]
    assert messages[1].message_id.startswith("msg_")


def test_a_message_stored_as_bare_chunk_ids_still_reads(
    store: PostgresThreadStore, pg_engine
) -> None:
    """Threads written before a citation carried its target must keep opening.

    The column is JSON text, so the old rows are still there verbatim; only the
    reader changed.
    """
    thread = store.create_thread(owner_user_id="user-a", title="Chat")
    store.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.ASSISTANT,
        content="Article 5 defines the scope.",
    )
    with pg_engine.begin() as connection:
        connection.execute(
            text("UPDATE thread_messages SET citations = :value"),
            {"value": '["chunk-1","chunk-2"]'},
        )

    messages = store.list_messages(owner_user_id="user-a", thread_id=thread.thread_id)

    assert messages is not None
    assert messages[0].citations == [
        Citation(chunk_id="chunk-1"),
        Citation(chunk_id="chunk-2"),
    ]
    assert messages[0].citations[0].doc_id is None


def test_adding_a_message_bumps_the_thread_timestamp(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Chat")

    store.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="Hello",
    )
    reloaded = store.get_thread(owner_user_id="user-a", thread_id=thread.thread_id)

    assert reloaded is not None
    assert reloaded.updated_at >= thread.updated_at


def test_messages_cannot_be_added_to_an_unknown_thread(store: PostgresThreadStore) -> None:
    assert store.add_message(
        owner_user_id="user-a",
        thread_id="thr_missing",
        role=ThreadMessageRole.USER,
        content="Hello",
    ) is None
    assert store.list_messages(owner_user_id="user-a", thread_id="thr_missing") is None


def test_deleting_a_thread_removes_its_messages(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Chat")
    store.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="Hello",
    )

    assert store.delete_thread(owner_user_id="user-a", thread_id=thread.thread_id) is True
    assert store.get_thread(owner_user_id="user-a", thread_id=thread.thread_id) is None
    assert store.list_messages(owner_user_id="user-a", thread_id=thread.thread_id) is None
    assert store.delete_thread(owner_user_id="user-a", thread_id=thread.thread_id) is False


def test_message_content_must_not_be_blank(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Chat")

    with pytest.raises(ValidationError):
        store.add_message(
            owner_user_id="user-a",
            thread_id=thread.thread_id,
            role=ThreadMessageRole.USER,
            content="",
        )


def test_thread_records_are_frozen(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Chat")

    with pytest.raises(ValidationError, match="frozen"):
        thread.project_id = "prj_cannot_be_reassigned"


def test_threads_survive_a_new_store_instance(pg_engine: Engine) -> None:
    first = PostgresThreadStore(pg_engine)
    thread = first.create_thread(owner_user_id="user-a", title="Persisted")
    first.add_message(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="Still here?",
    )

    second = PostgresThreadStore(pg_engine)
    messages = second.list_messages(
        owner_user_id="user-a", thread_id=thread.thread_id
    )
    assert messages is not None
    assert [item.content for item in messages] == ["Still here?"]


def test_a_new_thread_has_no_summary(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Fresh")

    assert thread.summary is None
    assert thread.summarized_through_position is None


def test_set_summary_stores_the_text_and_the_watermark(
    store: PostgresThreadStore,
) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Long chat")

    updated = store.set_summary(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        summary="They discussed articles 5 and 6.",
        summarized_through_position=10,
    )

    assert updated is not None
    assert updated.summary == "They discussed articles 5 and 6."
    assert updated.summarized_through_position == 10
    reloaded = store.get_thread(owner_user_id="user-a", thread_id=thread.thread_id)
    assert reloaded is not None
    assert reloaded.summary == "They discussed articles 5 and 6."
    assert reloaded.summarized_through_position == 10


def test_set_summary_does_not_move_the_thread_up_the_recents_list(
    store: PostgresThreadStore,
) -> None:
    """Summarizing is bookkeeping, not activity.

    It runs after the answer is already sent, so letting it bump `sequence`
    would reorder the sidebar for something the user never did.
    """
    older = store.create_thread(owner_user_id="user-a", title="Older")
    newer = store.create_thread(owner_user_id="user-a", title="Newer")

    store.set_summary(
        owner_user_id="user-a",
        thread_id=older.thread_id,
        summary="Anything.",
        summarized_through_position=2,
    )

    assert [item.title for item in store.list_threads(owner_user_id="user-a")] == [
        "Newer",
        "Older",
    ]


def test_set_summary_is_owner_scoped(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Private")

    assert (
        store.set_summary(
            owner_user_id="user-b",
            thread_id=thread.thread_id,
            summary="Stolen.",
            summarized_through_position=1,
        )
        is None
    )
    reloaded = store.get_thread(owner_user_id="user-a", thread_id=thread.thread_id)
    assert reloaded is not None
    assert reloaded.summary is None


def _fill(store: PostgresThreadStore, thread_id: str, count: int) -> None:
    for number in range(1, count + 1):
        store.add_message(
            owner_user_id="user-a",
            thread_id=thread_id,
            role=ThreadMessageRole.USER if number % 2 else ThreadMessageRole.ASSISTANT,
            content=f"message {number}",
        )


def test_messages_carry_their_conversation_position(
    store: PostgresThreadStore,
) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Positions")
    _fill(store, thread.thread_id, 3)

    messages = store.list_messages(owner_user_id="user-a", thread_id=thread.thread_id)

    assert messages is not None
    assert [item.position for item in messages] == [1, 2, 3]


def test_a_thread_past_the_limit_returns_its_newest_messages(
    store: PostgresThreadStore,
) -> None:
    """The window must hold the present, not the beginning.

    Ordering ascending under a LIMIT returned the 200 oldest messages, so every
    turn past 200 answered with the opening of the conversation as its context.
    """
    thread = store.create_thread(owner_user_id="user-a", title="Very long")
    _fill(store, thread.thread_id, 205)

    messages = store.list_messages(
        owner_user_id="user-a", thread_id=thread.thread_id, limit=200
    )

    assert messages is not None
    assert len(messages) == 200
    assert messages[-1].content == "message 205"
    assert messages[0].content == "message 6"
    # Still conversation order, not reversed.
    assert [item.position for item in messages] == list(range(6, 206))


def test_count_messages_sees_past_the_window(store: PostgresThreadStore) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Very long")
    _fill(store, thread.thread_id, 205)

    assert store.count_messages(owner_user_id="user-a", thread_id=thread.thread_id) == 205
    assert store.count_messages(owner_user_id="user-b", thread_id=thread.thread_id) is None


def test_list_messages_range_reads_the_middle_of_a_thread(
    store: PostgresThreadStore,
) -> None:
    """What the summarizer folds: the span just past its watermark."""
    thread = store.create_thread(owner_user_id="user-a", title="Very long")
    _fill(store, thread.thread_id, 205)

    fresh = store.list_messages_range(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        from_position=2,
        through_position=5,
    )

    assert fresh is not None
    assert [item.content for item in fresh] == [
        "message 2",
        "message 3",
        "message 4",
        "message 5",
    ]


def test_list_messages_range_is_owner_scoped_and_bounded(
    store: PostgresThreadStore,
) -> None:
    thread = store.create_thread(owner_user_id="user-a", title="Range")
    _fill(store, thread.thread_id, 10)

    assert (
        store.list_messages_range(
            owner_user_id="user-b",
            thread_id=thread.thread_id,
            from_position=1,
            through_position=5,
        )
        is None
    )
    capped = store.list_messages_range(
        owner_user_id="user-a",
        thread_id=thread.thread_id,
        from_position=1,
        through_position=10,
        limit=3,
    )
    assert capped is not None
    assert [item.position for item in capped] == [1, 2, 3]
    # An empty or inverted range is not an error, it is nothing to fold.
    assert (
        store.list_messages_range(
            owner_user_id="user-a",
            thread_id=thread.thread_id,
            from_position=8,
            through_position=4,
        )
        == []
    )


def test_recency_numbers_are_unique_across_open_transactions(
    pg_engine: Engine,
) -> None:
    """`max(sequence) + 1` let two concurrent writers claim one number.

    Two sessions, both open, both asking for the next number, is the race
    itself, and it is deterministic: under `max() + 1` neither can see the
    other's uncommitted row so both read the same maximum. Reaching into
    `_next_sequence` is deliberate, the counter has no public surface and
    thread ordering hides a tie behind whichever plan Postgres picks.
    """
    store = PostgresThreadStore(pg_engine)
    store.create_thread(owner_user_id="user-a", title="Seed")

    with Session(pg_engine) as first_session, Session(pg_engine) as second_session:
        first = store._next_sequence(first_session)
        second = store._next_sequence(second_session)

    assert first != second


def test_threads_touched_in_turn_list_in_reverse(
    store: PostgresThreadStore,
) -> None:
    threads = [
        store.create_thread(owner_user_id="user-a", title=f"Thread {number}")
        for number in range(6)
    ]
    for thread in threads:
        store.add_message(
            owner_user_id="user-a",
            thread_id=thread.thread_id,
            role=ThreadMessageRole.USER,
            content="touch",
        )

    listed = store.list_threads(owner_user_id="user-a")

    assert [item.title for item in listed] == [
        f"Thread {number}" for number in reversed(range(6))
    ]
