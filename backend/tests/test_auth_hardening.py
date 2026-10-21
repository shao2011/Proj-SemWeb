"""Server-side identity rules — nothing here relies on client behavior.

Two namespaces, separated by construction:
- anonymous ids MUST match ``anon_<32 hex>`` and are the only ids the
  X-User-ID fallback accepts;
- verified ids come exclusively from a Bearer token, and a token whose uid
  masquerades as an anonymous id is rejected outright.

Claiming moves an anonymous footprint into a verified account, so it refuses
any target that is not anonymous-shaped or that ever logged in itself.
"""

from __future__ import annotations

from rag_ingestion.storage.pg_users import PostgresUserStore

ANON = "anon_" + "a" * 32
VERIFIED = "firebase-uid-1"


class FakeVerifier:
    def __init__(self, uid: str = VERIFIED) -> None:
        self.uid = uid
        self.email = "user@example.com"
        self.display_name = "Real Name"

    def verify_bearer(self, authorization: str):
        from rag_ingestion.api.auth import FirebaseTokenError, VerifiedIdentity

        token = authorization.partition(" ")[2]
        if token != "good-token":
            raise FirebaseTokenError("bad token")
        return VerifiedIdentity(
            uid=self.uid, email=self.email, display_name=self.display_name
        )


def _client(pg_engine, verifier=None):
    from tests.test_thread_api import _setup

    return _setup(
        pg_engine,
        auth_verifier=verifier or FakeVerifier(),
        users=PostgresUserStore(pg_engine),
    )[0]


# --- fallback namespace -------------------------------------------------------


def test_fallback_accepts_only_anonymous_shaped_ids(pg_engine) -> None:
    client = _client(pg_engine)

    ok = client.get("/threads", headers={"X-User-ID": ANON})
    assert ok.status_code == 200

    for impersonation in [VERIFIED, "anon-uuid-1234", "anon_XYZ", "a" * 40]:
        refused = client.get("/threads", headers={"X-User-ID": impersonation})
        assert refused.status_code == 401, impersonation


def test_bearer_identity_wins_regardless_of_header(pg_engine) -> None:
    client = _client(pg_engine)

    response = client.post(
        "/threads",
        json={"title": "mine"},
        headers={"Authorization": "Bearer good-token", "X-User-ID": ANON},
    )
    assert response.status_code == 201
    assert response.json()["owner_user_id"] == VERIFIED


def test_a_token_uid_shaped_like_an_anonymous_id_is_rejected(pg_engine) -> None:
    client = _client(pg_engine, verifier=FakeVerifier(uid=ANON))

    response = client.get(
        "/threads", headers={"Authorization": "Bearer good-token"}
    )
    assert response.status_code == 401


# --- users registry -----------------------------------------------------------


def test_verified_requests_register_the_user(pg_engine) -> None:
    client = _client(pg_engine)
    users = PostgresUserStore(pg_engine)
    assert users.is_registered(VERIFIED) is False

    client.get("/threads", headers={"Authorization": "Bearer good-token"})

    assert users.is_registered(VERIFIED) is True


# --- claim guards --------------------------------------------------------------


def test_claim_refuses_targets_that_are_not_anonymous_shaped(pg_engine) -> None:
    client = _client(pg_engine)

    response = client.post(
        "/auth/claim",
        json={"anonymous_user_id": "some-other-users-uid"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 403


def test_claim_refuses_ids_that_ever_logged_in(pg_engine) -> None:
    users = PostgresUserStore(pg_engine)
    # An account whose uid is somehow anonymous-shaped must still be safe.
    users.record_login(uid=ANON, email=None, display_name=None)
    client = _client(pg_engine)

    response = client.post(
        "/auth/claim",
        json={"anonymous_user_id": ANON},
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 403


def test_claim_still_works_for_a_real_anonymous_id(pg_engine) -> None:
    from rag_ingestion.storage.pg_threads import PostgresThreadStore

    threads = PostgresThreadStore(pg_engine)
    threads.create_thread(owner_user_id=ANON, title="Anon chat", project_id=None)
    client = _client(pg_engine)

    response = client.post(
        "/auth/claim",
        json={"anonymous_user_id": ANON},
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 200
    assert response.json()["threads"] == 1
