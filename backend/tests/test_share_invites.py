"""Sharing a chat with named people, by email.

Two access modes on one share row. A `link` share is the public snapshot that
already existed. An `invite` share is readable only by the people the owner
named, matched against the *verified* email inside the caller's token -- never
against anything the client asserts, and never against an unverified address,
which anyone could claim.

An invited reader does not write into the owner's thread. Sending a message
forks the snapshot into a thread of their own, with `project_id` deliberately
NULL: that is what makes "the guest cannot retrieve over the owner's
documents" true by construction rather than by a flag someone has to remember.
"""

from __future__ import annotations

from rag_ingestion.api.auth import FirebaseTokenError, VerifiedIdentity
from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.pg_users import PostgresUserStore
from rag_ingestion.storage.threads import Citation, ThreadMessageRole

OWNER = "firebase-owner-1"
OWNER_TOKEN = "owner-token"
GUEST = "firebase-guest-1"
GUEST_TOKEN = "guest-token"
GUEST_EMAIL = "guest@example.com"
STRANGER_TOKEN = "stranger-token"
UNVERIFIED_TOKEN = "unverified-token"

OWNER_HEADERS = {"Authorization": f"Bearer {OWNER_TOKEN}", "X-User-ID": OWNER}
GUEST_HEADERS = {"Authorization": f"Bearer {GUEST_TOKEN}", "X-User-ID": GUEST}
STRANGER_HEADERS = {"Authorization": f"Bearer {STRANGER_TOKEN}", "X-User-ID": "other"}
UNVERIFIED_HEADERS = {
    "Authorization": f"Bearer {UNVERIFIED_TOKEN}",
    "X-User-ID": "unverified-uid",
}


class MultiVerifier:
    """A token per identity, so one client can act as several people."""

    IDENTITIES = {
        OWNER_TOKEN: VerifiedIdentity(
            uid=OWNER, email="owner@example.com", email_verified=True
        ),
        GUEST_TOKEN: VerifiedIdentity(
            uid=GUEST, email=GUEST_EMAIL, email_verified=True
        ),
        STRANGER_TOKEN: VerifiedIdentity(
            uid="firebase-stranger", email="stranger@example.com", email_verified=True
        ),
        # Same address as the invitation, but the provider never proved it.
        UNVERIFIED_TOKEN: VerifiedIdentity(
            uid="firebase-liar", email=GUEST_EMAIL, email_verified=False
        ),
    }

    def verify_bearer(self, authorization: str) -> VerifiedIdentity:
        token = authorization.partition(" ")[2]
        identity = self.IDENTITIES.get(token)
        if identity is None:
            raise FirebaseTokenError("bad token")
        return identity


def _client(pg_engine, **app_options):
    from tests.test_thread_api import _setup

    return _setup(
        pg_engine,
        auth_verifier=MultiVerifier(),
        users=PostgresUserStore(pg_engine),
        **app_options,
    )[0]


def _shared_thread(pg_engine, client, *, citations: bool = False) -> tuple[str, str]:
    """An owner thread with two messages, already shared. Returns (id, token)."""
    store = PostgresThreadStore(pg_engine)
    thread = store.create_thread(owner_user_id=OWNER, title="Planning")
    store.add_message(
        owner_user_id=OWNER,
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="What does the contract say?",
    )
    store.add_message(
        owner_user_id=OWNER,
        thread_id=thread.thread_id,
        role=ThreadMessageRole.ASSISTANT,
        content="It says ninety days [cite:1].",
        citations=(
            [Citation(chunk_id="chunk-secret", doc_id="doc-secret", block_ids=["b1"])]
            if citations
            else None
        ),
    )
    share = client.post(
        f"/threads/{thread.thread_id}/share", json={}, headers=OWNER_HEADERS
    )
    assert share.status_code == 201
    return thread.thread_id, share.json()["share_id"]


def _invite(client, thread_id: str, email: str = GUEST_EMAIL):
    return client.post(
        f"/threads/{thread_id}/share/recipients",
        json={"email": email},
        headers=OWNER_HEADERS,
    )


# --- access mode ----------------------------------------------------------------


def test_a_link_share_stays_public_until_someone_is_invited(pg_engine) -> None:
    client = _client(pg_engine)
    _thread_id, token = _shared_thread(pg_engine, client)

    assert client.get(f"/share/{token}").status_code == 200


def test_inviting_the_first_person_closes_the_link(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)

    invited = _invite(client, thread_id)

    assert invited.status_code == 201
    assert invited.json()["access"] == "invite"
    # The same URL that worked a moment ago now needs a login.
    assert client.get(f"/share/{token}").status_code == 401


def test_the_owner_can_reopen_the_link_to_anyone(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    reopened = client.post(
        f"/threads/{thread_id}/share", json={"access": "link"}, headers=OWNER_HEADERS
    )

    assert reopened.status_code == 200
    assert client.get(f"/share/{token}").status_code == 200


# --- who may read ---------------------------------------------------------------


def test_the_invited_address_can_read_it(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    opened = client.get(f"/share/{token}", headers=GUEST_HEADERS)

    assert opened.status_code == 200
    assert opened.json()["title"] == "Planning"
    assert opened.json()["can_continue"] is True


def test_another_signed_in_account_is_refused(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.get(f"/share/{token}", headers=STRANGER_HEADERS).status_code == 403


def test_an_unverified_address_never_matches(pg_engine) -> None:
    """Otherwise anyone could sign up claiming the invited address."""
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.get(f"/share/{token}", headers=UNVERIFIED_HEADERS).status_code == 403


def test_addresses_match_regardless_of_case_and_padding(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)

    _invite(client, thread_id, email="  GUEST@Example.COM  ")

    assert client.get(f"/share/{token}", headers=GUEST_HEADERS).status_code == 200


def test_the_owner_keeps_access_to_their_own_invite_share(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.get(f"/share/{token}", headers=OWNER_HEADERS).status_code == 200


# --- managing the list -----------------------------------------------------------


def test_the_owner_sees_and_can_remove_the_list(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    listed = client.get(f"/threads/{thread_id}/share", headers=OWNER_HEADERS)
    assert listed.json()["recipients"] == [GUEST_EMAIL]

    removed = client.request(
        "DELETE",
        f"/threads/{thread_id}/share/recipients",
        json={"email": GUEST_EMAIL},
        headers=OWNER_HEADERS,
    )

    assert removed.status_code == 204
    assert client.get(f"/share/{token}", headers=GUEST_HEADERS).status_code == 403


def test_inviting_the_same_address_twice_is_not_an_error(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, _token = _shared_thread(pg_engine, client)

    _invite(client, thread_id)
    again = _invite(client, thread_id)

    assert again.status_code == 201
    assert again.json()["recipients"] == [GUEST_EMAIL]


def test_a_malformed_address_is_rejected(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, _token = _shared_thread(pg_engine, client)

    assert _invite(client, thread_id, email="not-an-email").status_code == 422


def test_only_the_owner_can_invite(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, _token = _shared_thread(pg_engine, client)

    refused = client.post(
        f"/threads/{thread_id}/share/recipients",
        json={"email": "someone@example.com"},
        headers=GUEST_HEADERS,
    )

    assert refused.status_code == 404


# --- shared with you --------------------------------------------------------------


def test_shared_with_you_lists_what_my_address_was_invited_to(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    mine = client.get("/shared", headers=GUEST_HEADERS)

    assert mine.status_code == 200
    assert [item["share_id"] for item in mine.json()] == [token]
    assert mine.json()[0]["title"] == "Planning"


def test_shared_with_you_is_empty_for_everyone_else(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, _token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.get("/shared", headers=STRANGER_HEADERS).json() == []


def test_shared_with_you_needs_a_login(pg_engine) -> None:
    client = _client(pg_engine)

    assert client.get("/shared", headers={"X-User-ID": "anon_" + "a" * 32}).status_code == 401


# --- continuing in your own copy ---------------------------------------------------


def test_continuing_forks_the_snapshot_into_the_guests_own_thread(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client, citations=True)
    _invite(client, thread_id)

    forked = client.post(f"/share/{token}/fork", headers=GUEST_HEADERS)

    assert forked.status_code == 201
    body = forked.json()
    assert body["owner_user_id"] == GUEST
    assert body["thread_id"] != thread_id
    # NULL project is the whole safety story: retrieval cannot reach the
    # owner's documents because the copy belongs to no project at all.
    assert body["project_id"] is None

    copied = client.get(f"/threads/{body['thread_id']}", headers=GUEST_HEADERS).json()
    assert [item["content"] for item in copied["messages"]] == [
        "What does the contract say?",
        "It says ninety days [cite:1].",
    ]
    # The owner's chunk and document ids do not travel with the copy.
    assert all(item["citations"] == [] for item in copied["messages"])


def test_the_owners_thread_is_untouched_by_a_guest_continuing(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    client.post(f"/share/{token}/fork", headers=GUEST_HEADERS)

    original = client.get(f"/threads/{thread_id}", headers=OWNER_HEADERS).json()
    assert len(original["messages"]) == 2
    assert client.get("/threads", headers=OWNER_HEADERS).json().__len__() == 1


def test_a_stranger_cannot_fork_an_invite_share(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.post(f"/share/{token}/fork", headers=STRANGER_HEADERS).status_code == 403


def test_continuing_needs_a_login(pg_engine) -> None:
    client = _client(pg_engine)
    thread_id, token = _shared_thread(pg_engine, client)
    _invite(client, thread_id)

    assert client.post(f"/share/{token}/fork").status_code == 401


def test_a_public_link_is_read_only(pg_engine) -> None:
    """Only the invite flow was asked to be continuable; a link share stays
    the read-only snapshot it has always been."""
    client = _client(pg_engine)
    _thread_id, token = _shared_thread(pg_engine, client)

    assert client.get(f"/share/{token}").json()["can_continue"] is False
    assert client.post(f"/share/{token}/fork", headers=GUEST_HEADERS).status_code == 403
