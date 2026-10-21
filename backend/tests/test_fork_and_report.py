"""Forking a chat from a message, and reporting one.

Forking copies the conversation up to a chosen position into a new thread, so
the branch can go somewhere else without touching the original. Reporting
stores the chat as it was at the moment of the report, deliberately outliving
the thread it is about -- a report whose evidence can be deleted by the person
being reported is not a report.
"""

from __future__ import annotations

import json

from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.pg_users import PostgresUserStore
from rag_ingestion.storage.threads import ThreadMessageRole
from tests.test_auth_hardening import ANON, VERIFIED, FakeVerifier

BEARER = {"Authorization": "Bearer good-token", "X-User-ID": VERIFIED}


def _client(pg_engine, **app_options):
    from tests.test_thread_api import _setup

    return _setup(
        pg_engine,
        auth_verifier=FakeVerifier(),
        users=PostgresUserStore(pg_engine),
        **app_options,
    )[0]


def _thread_with_messages(
    pg_engine, count: int, owner: str = VERIFIED, project_id: str | None = None
) -> str:
    store = PostgresThreadStore(pg_engine)
    thread = store.create_thread(
        owner_user_id=owner, title="Original chat", project_id=project_id
    )
    for index in range(count):
        store.add_message(
            owner_user_id=owner,
            thread_id=thread.thread_id,
            role=(
                ThreadMessageRole.USER if index % 2 == 0 else ThreadMessageRole.ASSISTANT
            ),
            content=f"message {index + 1}",
        )
    return thread.thread_id


# --- fork ---------------------------------------------------------------------


def test_forking_requires_a_verified_login(pg_engine) -> None:
    """The anonymous allowance counts user messages, and a fork copies some.

    Letting an anonymous visitor fork would spend their trial on messages they
    never sent, so the refusal is the honest answer rather than a silent
    quota burn.
    """
    thread_id = _thread_with_messages(pg_engine, 2, owner=ANON)
    client = _client(pg_engine)

    refused = client.post(
        f"/threads/{thread_id}/fork",
        json={"through_position": 2},
        headers={"X-User-ID": ANON},
    )

    assert refused.status_code == 403
    assert refused.json()["detail"] == "login_required_to_fork"


def test_a_fork_copies_the_conversation_up_to_the_chosen_message(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 4)
    client = _client(pg_engine)

    forked = client.post(
        f"/threads/{thread_id}/fork", json={"through_position": 2}, headers=BEARER
    )

    assert forked.status_code == 201
    new_id = forked.json()["thread_id"]
    assert new_id != thread_id
    assert forked.json()["title"] == "Original chat (continued)"

    copied = client.get(f"/threads/{new_id}", headers=BEARER).json()["messages"]
    assert [item["content"] for item in copied] == ["message 1", "message 2"]
    assert [item["position"] for item in copied] == [1, 2]
    assert [item["role"] for item in copied] == ["user", "assistant"]
    # New rows, not the originals moved across.
    original = client.get(f"/threads/{thread_id}", headers=BEARER).json()["messages"]
    assert len(original) == 4
    assert {item["message_id"] for item in copied}.isdisjoint(
        {item["message_id"] for item in original}
    )


def test_a_fork_stays_in_the_same_project(pg_engine) -> None:
    from tests.test_thread_api import _create_project, _setup

    client, _, _qdrant, _bundle = _setup(
        pg_engine,
        auth_verifier=FakeVerifier(),
        users=PostgresUserStore(pg_engine),
    )
    project_id = client.post(
        "/projects", json={"name": "Legal", "provider": "cohere"}, headers=BEARER
    ).json()["project_id"]
    thread_id = _thread_with_messages(pg_engine, 2, project_id=project_id)

    forked = client.post(
        f"/threads/{thread_id}/fork", json={"through_position": 2}, headers=BEARER
    )

    assert forked.json()["project_id"] == project_id


def test_forking_someone_elses_thread_is_a_404(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2, owner="another-user")
    client = _client(pg_engine)

    refused = client.post(
        f"/threads/{thread_id}/fork", json={"through_position": 1}, headers=BEARER
    )

    assert refused.status_code == 404


def test_a_position_past_the_end_copies_what_is_there(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 3)
    client = _client(pg_engine)

    forked = client.post(
        f"/threads/{thread_id}/fork", json={"through_position": 99}, headers=BEARER
    )

    new_id = forked.json()["thread_id"]
    copied = client.get(f"/threads/{new_id}", headers=BEARER).json()["messages"]
    assert len(copied) == 3


def test_a_position_below_one_is_rejected(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    assert (
        client.post(
            f"/threads/{thread_id}/fork", json={"through_position": 0}, headers=BEARER
        ).status_code
        == 422
    )


def test_a_fork_starts_without_the_originals_summary(pg_engine) -> None:
    """The copy is a fresh conversation: every message it holds is verbatim,
    so a watermark inherited from the original would claim turns are already
    folded when they are not."""
    thread_id = _thread_with_messages(pg_engine, 2)
    PostgresThreadStore(pg_engine).set_summary(
        owner_user_id=VERIFIED,
        thread_id=thread_id,
        summary="[s1|u:0|f:0] something old",
        summarized_through_position=2,
    )
    client = _client(pg_engine)

    forked = client.post(
        f"/threads/{thread_id}/fork", json={"through_position": 2}, headers=BEARER
    ).json()

    assert forked["summary"] is None
    assert forked["summarized_through_position"] is None


# --- report -------------------------------------------------------------------


def test_a_report_is_stored_with_the_conversation(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    created = client.post(
        "/reports",
        json={
            "thread_id": thread_id,
            "message_position": 2,
            "reason": "harmful",
            "details": "Told me to do something unsafe.",
        },
        headers=BEARER,
    )

    assert created.status_code == 201
    assert created.json()["report_id"].startswith("rpt_")


def test_a_report_outlives_the_chat_it_is_about(pg_engine) -> None:
    from rag_ingestion.db.tables import MessageReportRow
    from sqlmodel import Session, select

    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    client.post(
        "/reports",
        json={"thread_id": thread_id, "message_position": 2, "reason": "harmful"},
        headers=BEARER,
    )

    assert client.delete(f"/threads/{thread_id}", headers=BEARER).status_code == 204

    with Session(pg_engine) as session:
        row = session.exec(
            select(MessageReportRow).where(MessageReportRow.thread_id == thread_id)
        ).one()
        assert [item["content"] for item in json.loads(row.conversation)] == [
            "message 1",
            "message 2",
        ]
        assert row.reason == "harmful"


def test_details_are_optional(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    created = client.post(
        "/reports",
        json={"thread_id": thread_id, "message_position": 1, "reason": "not_helpful"},
        headers=BEARER,
    )

    assert created.status_code == 201


def test_an_unknown_reason_is_rejected(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    refused = client.post(
        "/reports",
        json={"thread_id": thread_id, "reason": "because-i-said-so"},
        headers=BEARER,
    )

    assert refused.status_code == 422


def test_details_longer_than_the_counter_allows_are_rejected(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    refused = client.post(
        "/reports",
        json={"thread_id": thread_id, "reason": "other", "details": "x" * 1001},
        headers=BEARER,
    )

    assert refused.status_code == 422


def test_reporting_someone_elses_chat_is_a_404(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2, owner="another-user")
    client = _client(pg_engine)

    refused = client.post(
        "/reports",
        json={"thread_id": thread_id, "reason": "harmful"},
        headers=BEARER,
    )

    assert refused.status_code == 404


def test_an_anonymous_visitor_can_still_report(pg_engine) -> None:
    """Reporting harmful content must not be gated behind a login: the person
    most likely to see it in the trial is someone who has not signed up."""
    thread_id = _thread_with_messages(pg_engine, 2, owner=ANON)
    client = _client(pg_engine)

    created = client.post(
        "/reports",
        json={"thread_id": thread_id, "reason": "harmful"},
        headers={"X-User-ID": ANON},
    )

    assert created.status_code == 201
