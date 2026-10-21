"""The anonymous trial limit, enforced on the server.

The client keeps a courtesy counter, but the authoritative count lives here:
an anonymous identity gets `anon_message_limit` user messages in total
(across all its threads), then the message route answers 403 with a
machine-readable detail the frontend turns into the login modal. Verified
logins are never limited.
"""

from __future__ import annotations

from rag_ingestion.api.auth import VerifiedIdentity

ANON = "anon_" + "f" * 32
VERIFIED = "firebase-uid-1"


class FakeVerifier:
    def verify_bearer(self, authorization: str) -> VerifiedIdentity:
        return VerifiedIdentity(uid=VERIFIED)


def _client(pg_engine, **options):
    from tests.test_thread_api import RecordingChat, _setup

    return _setup(
        pg_engine,
        RecordingChat(["ok."]),
        auth_verifier=FakeVerifier(),
        **options,
    )[0]


def _send(client, thread_id: str, headers: dict[str, str]):
    return client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=headers,
    )


def test_anonymous_messages_stop_at_the_limit_across_threads(pg_engine) -> None:
    client = _client(pg_engine, anon_message_limit=3)
    headers = {"X-User-ID": ANON}
    first = client.post("/threads", json={"title": "a"}, headers=headers).json()
    second = client.post("/threads", json={"title": "b"}, headers=headers).json()

    assert _send(client, first["thread_id"], headers).status_code == 200
    assert _send(client, first["thread_id"], headers).status_code == 200
    # The count spans threads, so the third message lands in another thread
    # and the fourth is refused wherever it goes.
    assert _send(client, second["thread_id"], headers).status_code == 200

    refused = _send(client, second["thread_id"], headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "anonymous_message_limit_reached"


def test_verified_logins_are_never_limited(pg_engine) -> None:
    client = _client(pg_engine, anon_message_limit=1)
    headers = {"Authorization": "Bearer any", "X-User-ID": VERIFIED}
    thread = client.post("/threads", json={"title": "a"}, headers=headers).json()

    for _ in range(3):
        assert _send(client, thread["thread_id"], headers).status_code == 200


def test_a_zero_limit_disables_the_gate(pg_engine) -> None:
    client = _client(pg_engine, anon_message_limit=0)
    headers = {"X-User-ID": ANON}
    thread = client.post("/threads", json={"title": "a"}, headers=headers).json()

    for _ in range(4):
        assert _send(client, thread["thread_id"], headers).status_code == 200
