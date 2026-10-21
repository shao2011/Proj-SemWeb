"""Sharing a chat by link.

Two halves that must not leak into each other: creating or revoking a link is
an owner action behind a verified login, while reading one is public and must
work with no identity at all. The snapshot is a watermark -- messages written
after the share was made are not part of it -- and the public payload carries
none of the owner's internal ids.
"""

from __future__ import annotations

import pytest

from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.pg_users import PostgresUserStore
from rag_ingestion.storage.threads import ThreadMessageRole
from rag_ingestion.api.routers import share as share_router
from tests.test_auth_hardening import ANON, VERIFIED, FakeVerifier

# Both headers, the way the real client sends them: the token decides the
# identity, X-User-ID rides along on every request.
BEARER = {"Authorization": "Bearer good-token", "X-User-ID": VERIFIED}


def _client(pg_engine, **app_options):
    from tests.test_thread_api import _setup

    return _setup(
        pg_engine,
        auth_verifier=FakeVerifier(),
        users=PostgresUserStore(pg_engine),
        **app_options,
    )[0]


def _thread_with_messages(pg_engine, count: int, owner: str = VERIFIED) -> str:
    store = PostgresThreadStore(pg_engine)
    thread = store.create_thread(owner_user_id=owner, title="Shared chat")
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


# --- creating -----------------------------------------------------------------


def test_sharing_requires_a_verified_login(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2, owner=ANON)
    client = _client(pg_engine)

    refused = client.post(
        f"/threads/{thread_id}/share", json={}, headers={"X-User-ID": ANON}
    )

    assert refused.status_code == 403
    assert refused.json()["detail"] == "login_required_to_share"


def test_owner_gets_an_unguessable_token(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 4)
    client = _client(pg_engine)

    created = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER)

    assert created.status_code == 201
    body = created.json()
    assert body["share_id"].startswith("shr_")
    assert len(body["share_id"]) == len("shr_") + 32
    assert body["shared_through_position"] == 4
    assert body["message_count"] == 4


def test_sharing_someone_elses_thread_is_a_404(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2, owner="another-user")
    client = _client(pg_engine)

    refused = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER)

    assert refused.status_code == 404


def test_sharing_twice_returns_the_same_link(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    first = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER)
    second = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER)

    assert second.status_code == 200
    assert second.json()["share_id"] == first.json()["share_id"]


def test_the_current_share_can_be_read_back(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)

    missing = client.get(f"/threads/{thread_id}/share", headers=BEARER)
    created = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER)
    current = client.get(f"/threads/{thread_id}/share", headers=BEARER)

    assert missing.status_code == 404
    assert current.status_code == 200
    assert current.json()["share_id"] == created.json()["share_id"]


# --- reading ------------------------------------------------------------------


def test_a_link_is_readable_with_no_identity_at_all(pg_engine) -> None:
    """The public route is exempt from the auth middleware.

    With the fallback off -- the production setting -- every other route 401s
    without a token. A share link is handed to people who have no account, so
    it must not.
    """
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine, allow_x_user_id_fallback=False)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    assert client.get("/threads").status_code == 401

    public = client.get(f"/share/{token}")

    assert public.status_code == 200
    assert public.headers["X-Robots-Tag"] == "noindex, nofollow"
    body = public.json()
    assert body["title"] == "Shared chat"
    assert [item["content"] for item in body["messages"]] == ["message 1", "message 2"]
    assert body["messages"][0]["role"] == "user"


def test_the_public_payload_carries_none_of_the_owners_ids(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    body = client.get(f"/share/{token}").json()

    flat = str(body)
    assert VERIFIED not in flat
    assert thread_id not in flat
    for leaked in ("owner_user_id", "thread_id", "project_id", "message_id", "citations"):
        assert leaked not in flat


def test_an_unknown_token_is_a_404_that_is_still_marked_noindex(pg_engine) -> None:
    client = _client(pg_engine)

    missing = client.get("/share/shr_" + "0" * 32)

    assert missing.status_code == 404
    assert missing.headers["X-Robots-Tag"] == "noindex, nofollow"


# --- snapshot -----------------------------------------------------------------


def test_messages_written_after_sharing_stay_private(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    PostgresThreadStore(pg_engine).add_message(
        owner_user_id=VERIFIED,
        thread_id=thread_id,
        role=ThreadMessageRole.USER,
        content="secret follow-up",
    )

    body = client.get(f"/share/{token}").json()

    assert len(body["messages"]) == 2
    assert "secret follow-up" not in str(body)


def test_renaming_the_thread_does_not_change_the_shared_title(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    client.patch(
        f"/threads/{thread_id}", json={"title": "Renamed after sharing"}, headers=BEARER
    )

    assert client.get(f"/share/{token}").json()["title"] == "Shared chat"


def test_refresh_extends_the_snapshot_and_keeps_the_link(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]
    PostgresThreadStore(pg_engine).add_message(
        owner_user_id=VERIFIED,
        thread_id=thread_id,
        role=ThreadMessageRole.USER,
        content="message 3",
    )
    client.patch(f"/threads/{thread_id}", json={"title": "Newer title"}, headers=BEARER)

    refreshed = client.post(
        f"/threads/{thread_id}/share", json={"refresh": True}, headers=BEARER
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["share_id"] == token
    body = client.get(f"/share/{token}").json()
    assert len(body["messages"]) == 3
    assert body["title"] == "Newer title"


# --- revoking -----------------------------------------------------------------


def test_revoking_kills_the_link(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    revoked = client.delete(f"/threads/{thread_id}/share", headers=BEARER)

    assert revoked.status_code == 204
    assert client.get(f"/share/{token}").status_code == 404
    assert client.delete(f"/threads/{thread_id}/share", headers=BEARER).status_code == 404


def test_sharing_again_after_revoking_mints_a_new_token(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    first = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]
    client.delete(f"/threads/{thread_id}/share", headers=BEARER)

    second = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    assert second != first
    assert client.get(f"/share/{first}").status_code == 404
    assert client.get(f"/share/{second}").status_code == 200


def test_deleting_the_thread_kills_the_link(pg_engine) -> None:
    thread_id = _thread_with_messages(pg_engine, 2)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    assert client.delete(f"/threads/{thread_id}", headers=BEARER).status_code == 204
    assert client.get(f"/share/{token}").status_code == 404


# --- long threads ---------------------------------------------------------------


def test_a_thread_longer_than_the_cap_says_so_instead_of_truncating_silently(
    pg_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(share_router, "SHARE_MAX_MESSAGES", 3)
    thread_id = _thread_with_messages(pg_engine, 5)
    client = _client(pg_engine)
    token = client.post(f"/threads/{thread_id}/share", json={}, headers=BEARER).json()[
        "share_id"
    ]

    body = client.get(f"/share/{token}").json()

    assert body["truncated"] is True
    assert body["message_count"] == 5
    assert [item["content"] for item in body["messages"]] == [
        "message 1",
        "message 2",
        "message 3",
    ]
