"""Adding people to a project, with roles.

Three roles. A viewer reads the project and asks it questions. An editor also
uploads, trashes documents and renames. Only the owner deletes the project or
manages the member list, and only the owner hands ownership over -- which
demotes them to editor rather than throwing them out.

The load-bearing detail is the retrieval tenant. Chunks carry the payload of
whoever uploaded them, so every store and index call for a project has to run
as the *project's* owner rather than as the caller. A member querying with
their own id would match nothing and get a silent empty answer, which is the
worst possible failure here: it looks like the project has no documents.
"""

from __future__ import annotations

from rag_ingestion.api.auth import FirebaseTokenError, VerifiedIdentity
from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    OfficePathLocator,
    SourceType,
)
from rag_ingestion.storage.pg_users import PostgresUserStore

OWNER = "firebase-owner-1"
OWNER_EMAIL = "owner@example.com"
MEMBER = "firebase-member-1"
MEMBER_EMAIL = "member@example.com"
OTHER = "firebase-other-1"
OTHER_EMAIL = "other@example.com"

OWNER_HEADERS = {"Authorization": "Bearer owner-token", "X-User-ID": OWNER}
MEMBER_HEADERS = {"Authorization": "Bearer member-token", "X-User-ID": MEMBER}
OTHER_HEADERS = {"Authorization": "Bearer other-token", "X-User-ID": OTHER}
ANON = "anon_" + "a" * 32


class Verifier:
    IDENTITIES = {
        "owner-token": VerifiedIdentity(
            uid=OWNER, email=OWNER_EMAIL, email_verified=True
        ),
        "member-token": VerifiedIdentity(
            uid=MEMBER, email=MEMBER_EMAIL, email_verified=True
        ),
        "other-token": VerifiedIdentity(
            uid=OTHER, email=OTHER_EMAIL, email_verified=True
        ),
        # Same address as the invitation, never proved by the provider.
        "unverified-token": VerifiedIdentity(
            uid="firebase-liar", email=MEMBER_EMAIL, email_verified=False
        ),
    }

    def verify_bearer(self, authorization: str) -> VerifiedIdentity:
        identity = self.IDENTITIES.get(authorization.partition(" ")[2])
        if identity is None:
            raise FirebaseTokenError("bad token")
        return identity


def _setup(pg_engine):
    from tests.test_thread_api import _setup as base

    client, _reranker, qdrant, bundle = base(
        pg_engine, auth_verifier=Verifier(), users=PostgresUserStore(pg_engine)
    )
    # Everyone who will act has an account, which is what ownership transfer
    # resolves an address against.
    users = PostgresUserStore(pg_engine)
    for uid, email in [
        (OWNER, OWNER_EMAIL),
        (MEMBER, MEMBER_EMAIL),
        (OTHER, OTHER_EMAIL),
    ]:
        users.record_login(uid=uid, email=email, display_name=None)
    return client, qdrant, bundle


def _project(client, headers=OWNER_HEADERS) -> str:
    created = client.post(
        "/projects", json={"name": "Legal", "provider": "cohere"}, headers=headers
    )
    assert created.status_code == 201, created.text
    return created.json()["project_id"]


def _index_a_document(qdrant, bundle, owner_user_id: str, project_id: str) -> None:
    """One indexed chunk, under the project owner's tenant."""
    document = Document.from_content(
        content=b"members fixture",
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title="Policy handbook",
        domain="legal",
    )
    block = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.PARAGRAPH,
        text="Travel requests require code ZX-42.",
        confidence=0.98,
        language="en",
        section_path=["Travel"],
        locators=[OfficePathLocator(part="word/document.xml", path="/document/body/p[1]")],
    )
    chunks = chunk_document(
        document,
        [block],
        config=ChunkingConfig(
            leaf_min_tokens=1, leaf_target_tokens=64, leaf_max_tokens=128
        ),
    )
    QdrantHybridIndex(qdrant, bundle.collection_name, bundle.profile).upsert(
        chunks.chunks, bundle.embedder
    )


def _invite(client, project_id: str, email: str, role: str = "viewer"):
    return client.post(
        f"/projects/{project_id}/members",
        json={"email": email, "role": role},
        headers=OWNER_HEADERS,
    )


# --- inviting -------------------------------------------------------------------


def test_the_owner_invites_someone_with_a_role(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    invited = _invite(client, project_id, MEMBER_EMAIL, "editor")

    assert invited.status_code == 201
    assert invited.json() == [{"email": MEMBER_EMAIL, "role": "editor"}]


def test_addresses_are_matched_case_insensitively(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    _invite(client, project_id, "  MEMBER@Example.COM  ")

    assert client.get(
        f"/projects/{project_id}/documents", headers=MEMBER_HEADERS
    ).status_code == 200


def test_inviting_again_updates_the_role_instead_of_duplicating(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    _invite(client, project_id, MEMBER_EMAIL, "viewer")
    again = _invite(client, project_id, MEMBER_EMAIL, "editor")

    assert again.json() == [{"email": MEMBER_EMAIL, "role": "editor"}]


def test_an_anonymous_owner_cannot_invite(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client, headers={"X-User-ID": ANON})

    refused = client.post(
        f"/projects/{project_id}/members",
        json={"email": MEMBER_EMAIL, "role": "viewer"},
        headers={"X-User-ID": ANON},
    )

    assert refused.status_code == 403
    assert refused.json()["detail"] == "login_required_to_share_project"


def test_only_the_owner_manages_the_member_list(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")

    refused = client.post(
        f"/projects/{project_id}/members",
        json={"email": OTHER_EMAIL, "role": "viewer"},
        headers=MEMBER_HEADERS,
    )

    # 403, not 404: an editor already knows the project exists, so hiding it
    # would only be confusing. Outsiders still get a 404.
    assert refused.status_code == 403


def test_an_unknown_role_is_rejected(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    assert _invite(client, project_id, MEMBER_EMAIL, "admin").status_code == 422


def test_an_unverified_address_grants_nothing(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")

    refused = client.get(
        f"/projects/{project_id}/documents",
        headers={"Authorization": "Bearer unverified-token", "X-User-ID": "firebase-liar"},
    )

    assert refused.status_code == 404


# --- what each role may do --------------------------------------------------------


def test_a_viewer_reads_and_queries_but_changes_nothing(pg_engine) -> None:
    client, qdrant, bundle = _setup(pg_engine)
    project_id = _project(client)
    _index_a_document(qdrant, bundle, OWNER, project_id)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")

    assert client.get(
        f"/projects/{project_id}/documents", headers=MEMBER_HEADERS
    ).status_code == 200
    # The point of the whole feature: a member's question reaches the owner's
    # documents. An empty hit list here would look like an empty project.
    queried = client.post(
        f"/projects/{project_id}/query",
        json={"query": "ZX-42", "language": "en", "limit": 5},
        headers=MEMBER_HEADERS,
    )
    assert queried.status_code == 200
    assert queried.json()["hits"][0]["source_text"] == (
        "Travel requests require code ZX-42."
    )

    assert client.patch(
        f"/projects/{project_id}", json={"name": "Renamed"}, headers=MEMBER_HEADERS
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/documents", files={"files": ("a.txt", b"hi", "text/plain")},
        headers=MEMBER_HEADERS,
    ).status_code == 403
    assert client.delete(
        f"/projects/{project_id}", headers=MEMBER_HEADERS
    ).status_code == 403


def test_an_editor_renames_and_manages_documents_but_cannot_delete_the_project(
    pg_engine,
) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")

    renamed = client.patch(
        f"/projects/{project_id}", json={"name": "Renamed by editor"},
        headers=MEMBER_HEADERS,
    )

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed by editor"
    assert client.delete(
        f"/projects/{project_id}", headers=MEMBER_HEADERS
    ).status_code == 403


def test_a_stranger_sees_nothing(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    assert client.get(
        f"/projects/{project_id}/documents", headers=OTHER_HEADERS
    ).status_code == 404
    assert client.post(
        f"/projects/{project_id}/query",
        json={"query": "anything"},
        headers=OTHER_HEADERS,
    ).status_code == 404


def test_removing_a_member_removes_their_access(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")

    removed = client.request(
        "DELETE",
        f"/projects/{project_id}/members",
        json={"email": MEMBER_EMAIL},
        headers=OWNER_HEADERS,
    )

    assert removed.status_code == 204
    assert client.get(
        f"/projects/{project_id}/documents", headers=MEMBER_HEADERS
    ).status_code == 404


# --- listing ----------------------------------------------------------------------


def test_the_project_list_carries_the_callers_role(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")

    mine = client.get("/projects", headers=OWNER_HEADERS).json()
    theirs = client.get("/projects", headers=MEMBER_HEADERS).json()

    assert [(item["project_id"], item["role"]) for item in mine] == [
        (project_id, "owner")
    ]
    # Shared projects arrive in the same list, so the sidebar needs no second
    # request and the screen can split them by role.
    assert [(item["project_id"], item["role"]) for item in theirs] == [
        (project_id, "viewer")
    ]


def test_the_list_says_how_many_people_a_project_is_shared_with(pg_engine) -> None:
    """The owner's screen shows a lock and the word "Private"; it must stop
    saying that the moment somebody is added."""
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    before = client.get("/projects", headers=OWNER_HEADERS).json()
    assert before[0]["member_count"] == 0

    _invite(client, project_id, MEMBER_EMAIL, "viewer")
    _invite(client, project_id, OTHER_EMAIL, "editor")

    after = client.get("/projects", headers=OWNER_HEADERS).json()
    assert after[0]["member_count"] == 2


def test_a_stranger_sees_no_projects(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    _project(client)

    assert client.get("/projects", headers=OTHER_HEADERS).json() == []


# --- ownership transfer ------------------------------------------------------------


def test_transfer_promotes_the_member_and_demotes_the_owner(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")

    transferred = client.post(
        f"/projects/{project_id}/transfer",
        json={"email": MEMBER_EMAIL},
        headers=OWNER_HEADERS,
    )

    assert transferred.status_code == 200
    assert transferred.json()["owner_user_id"] == MEMBER
    # The old owner stays, as an editor, rather than losing the project.
    members = client.get(f"/projects/{project_id}/members", headers=MEMBER_HEADERS)
    assert members.json() == [{"email": OWNER_EMAIL, "role": "editor"}]


def test_after_transfer_the_new_owner_holds_the_owner_only_powers(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")
    client.post(
        f"/projects/{project_id}/transfer",
        json={"email": MEMBER_EMAIL},
        headers=OWNER_HEADERS,
    )

    # The old owner can still edit, but not delete or invite.
    assert client.patch(
        f"/projects/{project_id}", json={"name": "Still editable"},
        headers=OWNER_HEADERS,
    ).status_code == 200
    assert client.delete(
        f"/projects/{project_id}", headers=OWNER_HEADERS
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/members",
        json={"email": OTHER_EMAIL, "role": "viewer"},
        headers=OWNER_HEADERS,
    ).status_code == 403
    assert client.delete(
        f"/projects/{project_id}", headers=MEMBER_HEADERS
    ).status_code == 204


def test_retrieval_survives_a_transfer(pg_engine) -> None:
    """The chunks were indexed under the old owner's payload.

    Nothing about a transfer should make a project's own documents
    unreachable, so the payloads move with it.
    """
    client, qdrant, bundle = _setup(pg_engine)
    project_id = _project(client)
    _index_a_document(qdrant, bundle, OWNER, project_id)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")
    client.post(
        f"/projects/{project_id}/transfer",
        json={"email": MEMBER_EMAIL},
        headers=OWNER_HEADERS,
    )

    queried = client.post(
        f"/projects/{project_id}/query",
        json={"query": "ZX-42", "language": "en", "limit": 5},
        headers=MEMBER_HEADERS,
    )

    assert queried.status_code == 200
    assert queried.json()["hits"], "the project lost its own documents"


def test_transfer_needs_an_address_that_has_an_account(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)

    refused = client.post(
        f"/projects/{project_id}/transfer",
        json={"email": "never-signed-up@example.com"},
        headers=OWNER_HEADERS,
    )

    assert refused.status_code == 404


def test_only_the_owner_transfers(pg_engine) -> None:
    client, _qdrant, _bundle = _setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")

    refused = client.post(
        f"/projects/{project_id}/transfer",
        json={"email": OTHER_EMAIL},
        headers=MEMBER_HEADERS,
    )

    assert refused.status_code == 403


# --- chatting in a shared project --------------------------------------------------


def _chat_setup(pg_engine):
    """Like `_setup`, but with a fake chat provider so the SSE turn can run."""
    from tests.test_thread_api import RecordingChat, _setup as base

    chat = RecordingChat(["Ninety ", "days."])
    client, _reranker, qdrant, bundle = base(
        pg_engine,
        chat,
        auth_verifier=Verifier(),
        users=PostgresUserStore(pg_engine),
    )
    users = PostgresUserStore(pg_engine)
    for uid, email in [(OWNER, OWNER_EMAIL), (MEMBER, MEMBER_EMAIL)]:
        users.record_login(uid=uid, email=email, display_name=None)
    return client, qdrant, bundle


def test_a_member_chats_in_the_shared_project_against_the_owners_documents(
    pg_engine,
) -> None:
    """The headline flow, end to end through the streaming turn.

    `/query` already covers retrieval, but the chat route is a different code
    path: it resolves the project inside the SSE generator, and a failure there
    surfaces as an `error` event rather than a status code. So this asserts on
    the `sources` event, which is the only place the empty-project failure mode
    would show.
    """
    from tests.test_thread_api import _events

    client, qdrant, bundle = _chat_setup(pg_engine)
    project_id = _project(client)
    _index_a_document(qdrant, bundle, OWNER, project_id)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")

    thread = client.post(
        "/threads",
        json={"title": "Member chat", "project_id": project_id},
        headers=MEMBER_HEADERS,
    )
    assert thread.status_code == 201, thread.text
    # The thread belongs to the member, not to the project's owner.
    assert thread.json()["owner_user_id"] == MEMBER

    streamed = client.post(
        f"/threads/{thread.json()['thread_id']}/messages",
        json={"content": "How long is the notice period?"},
        headers=MEMBER_HEADERS,
    )

    assert streamed.status_code == 200
    events = dict(_events(streamed.text))
    assert "error" not in events, events.get("error")
    assert events["sources"]["hits"], "the member's turn retrieved nothing"
    assert events["sources"]["hits"][0]["source_text"] == (
        "Travel requests require code ZX-42."
    )


def test_the_owner_never_sees_a_members_thread(pg_engine) -> None:
    client, _qdrant, _bundle = _chat_setup(pg_engine)
    project_id = _project(client)
    _invite(client, project_id, MEMBER_EMAIL, "editor")
    client.post(
        "/threads",
        json={"title": "Member chat", "project_id": project_id},
        headers=MEMBER_HEADERS,
    )

    assert client.get("/threads", headers=OWNER_HEADERS).json() == []


def test_a_removed_member_cannot_keep_chatting_in_the_project(pg_engine) -> None:
    """Their thread survives, but the turn refuses instead of answering."""
    from tests.test_thread_api import _events

    client, qdrant, bundle = _chat_setup(pg_engine)
    project_id = _project(client)
    _index_a_document(qdrant, bundle, OWNER, project_id)
    _invite(client, project_id, MEMBER_EMAIL, "viewer")
    thread_id = client.post(
        "/threads",
        json={"title": "Member chat", "project_id": project_id},
        headers=MEMBER_HEADERS,
    ).json()["thread_id"]

    client.request(
        "DELETE",
        f"/projects/{project_id}/members",
        json={"email": MEMBER_EMAIL},
        headers=OWNER_HEADERS,
    )

    streamed = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Anything left?"},
        headers=MEMBER_HEADERS,
    )

    events = dict(_events(streamed.text))
    assert "sources" not in events
    assert events["error"]["detail"] == "Project not found"
