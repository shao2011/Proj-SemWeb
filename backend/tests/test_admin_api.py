"""The admin surface: users, reports inbox, admin grants, audit trail.

Two kinds of administrator. Bootstrap admins come from `ADMIN_EMAILS` and
exist before any database row does — that is what solves "who makes the first
admin". Granted admins are rows in `admin_users`, appointed from inside the
app. The API refuses to revoke or disable a bootstrap admin, so no sequence
of admin-screen actions can lock everybody out.

Disabling is enforced in the auth middleware, not per route: a disabled
account's valid token gets 403 on everything.
"""

from __future__ import annotations

from rag_ingestion.api.auth import FirebaseTokenError, VerifiedIdentity
from rag_ingestion.storage.pg_admin import PostgresAdminStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.pg_users import PostgresUserStore
from rag_ingestion.storage.threads import ThreadMessageRole

ROOT = "firebase-root-1"
ROOT_EMAIL = "root@example.com"
ALICE = "firebase-alice-1"
ALICE_EMAIL = "alice@example.com"
BOB = "firebase-bob-1"
BOB_EMAIL = "bob@example.com"

ROOT_HEADERS = {"Authorization": "Bearer root-token", "X-User-ID": ROOT}
ALICE_HEADERS = {"Authorization": "Bearer alice-token", "X-User-ID": ALICE}
BOB_HEADERS = {"Authorization": "Bearer bob-token", "X-User-ID": BOB}
ANON_HEADERS = {"X-User-ID": "anon_" + "a" * 32}


class Verifier:
    IDENTITIES = {
        "root-token": VerifiedIdentity(
            uid=ROOT, email=ROOT_EMAIL, email_verified=True
        ),
        "alice-token": VerifiedIdentity(
            uid=ALICE, email=ALICE_EMAIL, email_verified=True
        ),
        "bob-token": VerifiedIdentity(uid=BOB, email=BOB_EMAIL, email_verified=True),
        # Claims the bootstrap admin's address, never proved by the provider.
        "liar-token": VerifiedIdentity(
            uid="firebase-liar", email=ROOT_EMAIL, email_verified=False
        ),
    }

    def verify_bearer(self, authorization: str) -> VerifiedIdentity:
        identity = self.IDENTITIES.get(authorization.partition(" ")[2])
        if identity is None:
            raise FirebaseTokenError("bad token")
        return identity


def _setup(pg_engine):
    from tests.test_thread_api import _setup as base

    client, _reranker, _qdrant, _bundle = base(
        pg_engine,
        auth_verifier=Verifier(),
        users=PostgresUserStore(pg_engine),
        admins=PostgresAdminStore(pg_engine),
        admin_emails=(ROOT_EMAIL,),
    )
    users = PostgresUserStore(pg_engine)
    for uid, email in [(ROOT, ROOT_EMAIL), (ALICE, ALICE_EMAIL), (BOB, BOB_EMAIL)]:
        users.record_login(uid=uid, email=email, display_name=None)
    return client


def _report_from(pg_engine, client, headers=ALICE_HEADERS, owner=ALICE) -> str:
    store = PostgresThreadStore(pg_engine)
    thread = store.create_thread(owner_user_id=owner, title="Chat", project_id=None)
    store.add_message(
        owner_user_id=owner,
        thread_id=thread.thread_id,
        role=ThreadMessageRole.USER,
        content="tell me something unsafe",
    )
    store.add_message(
        owner_user_id=owner,
        thread_id=thread.thread_id,
        role=ThreadMessageRole.ASSISTANT,
        content="something unsafe",
    )
    created = client.post(
        "/reports",
        json={"thread_id": thread.thread_id, "reason": "harmful"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["report_id"]


# --- who gets in ----------------------------------------------------------------


def test_a_normal_user_gets_403_on_every_admin_route(pg_engine) -> None:
    client = _setup(pg_engine)
    for method, path in [
        ("GET", "/admin/me"),
        ("GET", "/admin/users"),
        ("GET", "/admin/reports"),
        ("GET", "/admin/admins"),
        ("GET", "/admin/audit"),
        ("POST", f"/admin/users/{BOB}/disable"),
    ]:
        response = client.request(method, path, headers=ALICE_HEADERS)
        assert response.status_code == 403, f"{method} {path}: {response.status_code}"


def test_an_anonymous_visitor_gets_403(pg_engine) -> None:
    client = _setup(pg_engine)
    assert client.get("/admin/users", headers=ANON_HEADERS).status_code == 403


def test_an_unverified_claim_to_the_admin_address_gets_403(pg_engine) -> None:
    client = _setup(pg_engine)
    headers = {"Authorization": "Bearer liar-token", "X-User-ID": "firebase-liar"}
    assert client.get("/admin/users", headers=headers).status_code == 403


def test_admin_me_names_the_bootstrap_admin(pg_engine) -> None:
    client = _setup(pg_engine)
    me = client.get("/admin/me", headers=ROOT_HEADERS)
    assert me.status_code == 200
    assert me.json() == {"email": ROOT_EMAIL, "bootstrap": True}


# --- users ------------------------------------------------------------------------


def test_the_user_list_shows_every_account(pg_engine) -> None:
    client = _setup(pg_engine)
    listed = client.get("/admin/users", headers=ROOT_HEADERS)
    assert listed.status_code == 200
    emails = {user["email"] for user in listed.json()}
    assert {ROOT_EMAIL, ALICE_EMAIL, BOB_EMAIL} <= emails
    alice = next(u for u in listed.json() if u["email"] == ALICE_EMAIL)
    assert alice["uid"] == ALICE
    assert alice["disabled_at"] is None
    assert alice["usage"]["total_tokens"] == 0


def test_disabling_locks_the_account_out_everywhere(pg_engine) -> None:
    client = _setup(pg_engine)
    assert client.get("/threads", headers=ALICE_HEADERS).status_code == 200

    disabled = client.post(f"/admin/users/{ALICE}/disable", headers=ROOT_HEADERS)
    assert disabled.status_code == 200
    assert disabled.json()["disabled_at"] is not None

    refused = client.get("/threads", headers=ALICE_HEADERS)
    assert refused.status_code == 403
    assert "disabled" in refused.json()["detail"].casefold()

    enabled = client.post(f"/admin/users/{ALICE}/enable", headers=ROOT_HEADERS)
    assert enabled.status_code == 200
    assert enabled.json()["disabled_at"] is None
    assert client.get("/threads", headers=ALICE_HEADERS).status_code == 200


def test_disabling_a_bootstrap_admin_is_refused(pg_engine) -> None:
    client = _setup(pg_engine)
    refused = client.post(f"/admin/users/{ROOT}/disable", headers=ROOT_HEADERS)
    assert refused.status_code == 403
    assert client.get("/admin/me", headers=ROOT_HEADERS).status_code == 200


def test_disabling_an_unknown_uid_is_404(pg_engine) -> None:
    client = _setup(pg_engine)
    assert (
        client.post("/admin/users/no-such-uid/disable", headers=ROOT_HEADERS).status_code
        == 404
    )


# --- admin grants -------------------------------------------------------------------


def test_granting_admin_lets_the_grantee_in_and_revoking_shuts_them_out(
    pg_engine,
) -> None:
    client = _setup(pg_engine)
    assert client.get("/admin/users", headers=BOB_HEADERS).status_code == 403

    granted = client.post(
        "/admin/admins", json={"email": "Bob@Example.COM  "}, headers=ROOT_HEADERS
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["email"] == BOB_EMAIL
    assert granted.json()["granted_by"] == ROOT_EMAIL

    assert client.get("/admin/users", headers=BOB_HEADERS).status_code == 200
    me = client.get("/admin/me", headers=BOB_HEADERS).json()
    assert me == {"email": BOB_EMAIL, "bootstrap": False}

    revoked = client.delete(f"/admin/admins/{BOB_EMAIL}", headers=ROOT_HEADERS)
    assert revoked.status_code == 204
    assert client.get("/admin/users", headers=BOB_HEADERS).status_code == 403


def test_the_admin_list_separates_bootstrap_from_granted(pg_engine) -> None:
    client = _setup(pg_engine)
    client.post("/admin/admins", json={"email": BOB_EMAIL}, headers=ROOT_HEADERS)
    listed = client.get("/admin/admins", headers=ROOT_HEADERS).json()
    assert listed["bootstrap"] == [ROOT_EMAIL]
    assert [entry["email"] for entry in listed["granted"]] == [BOB_EMAIL]


def test_granting_twice_or_granting_a_bootstrap_admin_is_409(pg_engine) -> None:
    client = _setup(pg_engine)
    client.post("/admin/admins", json={"email": BOB_EMAIL}, headers=ROOT_HEADERS)
    assert (
        client.post(
            "/admin/admins", json={"email": BOB_EMAIL}, headers=ROOT_HEADERS
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/admin/admins", json={"email": ROOT_EMAIL}, headers=ROOT_HEADERS
        ).status_code
        == 409
    )


def test_revoking_a_bootstrap_admin_is_refused(pg_engine) -> None:
    client = _setup(pg_engine)
    assert (
        client.delete(f"/admin/admins/{ROOT_EMAIL}", headers=ROOT_HEADERS).status_code
        == 403
    )


def test_revoking_a_non_admin_is_404(pg_engine) -> None:
    client = _setup(pg_engine)
    assert (
        client.delete(f"/admin/admins/{ALICE_EMAIL}", headers=ROOT_HEADERS).status_code
        == 404
    )


# --- reports inbox --------------------------------------------------------------------


def test_the_inbox_lists_open_reports_with_their_conversation(pg_engine) -> None:
    client = _setup(pg_engine)
    report_id = _report_from(pg_engine, client)

    inbox = client.get("/admin/reports", headers=ROOT_HEADERS)
    assert inbox.status_code == 200
    reports = inbox.json()
    assert [r["report_id"] for r in reports] == [report_id]
    report = reports[0]
    assert report["reason"] == "harmful"
    assert report["resolved_at"] is None
    assert report["conversation"] == [
        {"role": "user", "content": "tell me something unsafe"},
        {"role": "assistant", "content": "something unsafe"},
    ]


def test_resolving_a_report_removes_it_from_the_open_inbox(pg_engine) -> None:
    client = _setup(pg_engine)
    report_id = _report_from(pg_engine, client)

    resolved = client.post(
        f"/admin/reports/{report_id}/resolve", headers=ROOT_HEADERS
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_by"] == ROOT_EMAIL
    assert resolved.json()["resolved_at"] is not None

    assert client.get("/admin/reports", headers=ROOT_HEADERS).json() == []
    everything = client.get(
        "/admin/reports", params={"include_resolved": "true"}, headers=ROOT_HEADERS
    ).json()
    assert [r["report_id"] for r in everything] == [report_id]

    # Resolving twice changes nothing: the first resolver stays on record.
    again = client.post(f"/admin/reports/{report_id}/resolve", headers=ROOT_HEADERS)
    assert again.status_code == 200
    assert again.json()["resolved_by"] == ROOT_EMAIL


def test_resolving_an_unknown_report_is_404(pg_engine) -> None:
    client = _setup(pg_engine)
    assert (
        client.post("/admin/reports/rpt_missing/resolve", headers=ROOT_HEADERS).status_code
        == 404
    )


# --- audit trail ------------------------------------------------------------------------


def test_every_state_change_writes_an_audit_row(pg_engine) -> None:
    client = _setup(pg_engine)
    report_id = _report_from(pg_engine, client)

    client.post("/admin/admins", json={"email": BOB_EMAIL}, headers=ROOT_HEADERS)
    client.post(f"/admin/users/{ALICE}/disable", headers=ROOT_HEADERS)
    client.post(f"/admin/users/{ALICE}/enable", headers=ROOT_HEADERS)
    client.post(f"/admin/reports/{report_id}/resolve", headers=ROOT_HEADERS)
    # A second, no-op resolve must not write a second row.
    client.post(f"/admin/reports/{report_id}/resolve", headers=ROOT_HEADERS)
    client.delete(f"/admin/admins/{BOB_EMAIL}", headers=ROOT_HEADERS)

    trail = client.get("/admin/audit", headers=ROOT_HEADERS)
    assert trail.status_code == 200
    entries = trail.json()
    assert [(entry["action"], entry["target"]) for entry in entries] == [
        ("revoke_admin", BOB_EMAIL),
        ("resolve_report", report_id),
        ("enable_user", ALICE),
        ("disable_user", ALICE),
        ("grant_admin", BOB_EMAIL),
    ]
    assert all(entry["actor_email"] == ROOT_EMAIL for entry in entries)


def test_refused_actions_write_no_audit_row(pg_engine) -> None:
    client = _setup(pg_engine)
    client.post(f"/admin/users/{ROOT}/disable", headers=ROOT_HEADERS)
    client.delete(f"/admin/admins/{ROOT_EMAIL}", headers=ROOT_HEADERS)
    assert client.get("/admin/audit", headers=ROOT_HEADERS).json() == []


# --- overview stats -------------------------------------------------------------------


def test_stats_aggregate_the_whole_system(pg_engine) -> None:
    from datetime import UTC, datetime

    from sqlmodel import Session

    from rag_ingestion.db.tables import LlmUsageRow

    client = _setup(pg_engine)
    _report_from(pg_engine, client)
    with Session(pg_engine) as session:
        session.add(
            LlmUsageRow(
                owner_user_id=ALICE,
                prompt_tokens=700,
                completion_tokens=300,
                total_tokens=1000,
                requests=4,
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()

    stats = client.get("/admin/stats", headers=ROOT_HEADERS)
    assert stats.status_code == 200
    body = stats.json()
    assert body["users"] == {"total": 3, "active_7d": 3, "disabled": 0}
    assert body["usage"] == {
        "prompt_tokens": 700,
        "completion_tokens": 300,
        "total_tokens": 1000,
        "requests": 4,
    }
    assert body["content"]["threads"] == 1
    assert body["content"]["messages"] == 2
    assert body["content"]["projects"] == 0
    assert body["content"]["documents"] == 0
    assert body["content"]["documents_failed"] == 0
    assert body["reports"] == {"open": 1, "resolved": 0}
    assert body["top_users"] == [
        {
            "email": ALICE_EMAIL,
            "display_name": None,
            "total_tokens": 1000,
            "requests": 4,
        }
    ]


def test_stats_are_admin_only(pg_engine) -> None:
    client = _setup(pg_engine)
    assert client.get("/admin/stats", headers=ALICE_HEADERS).status_code == 403
