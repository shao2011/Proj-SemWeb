"""Claiming an anonymous account after the first real login.

The frontend keeps a random UUID as the identity until Google login lands a
Firebase uid. Claiming moves everything the anonymous id owns — projects,
threads, memory, settings, usage totals, and the Qdrant vectors' tenant
payload — to the verified uid, so "log in to keep your history" is true.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlmodel import Session, select

from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.db.tables import LlmUsageRow, UserSettingsRow
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.models import Block, BlockType, Document, OfficePathLocator, SourceType
from rag_ingestion.memory.models import MemoryScope, MemorySource, MemoryType
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore

ANON = "anon_" + "1234" * 8
VERIFIED = "firebase-uid-1"

PROFILE = EmbeddingProfile(
    provider="cohere",
    model="fake-2d",
    version="v1",
    dimensions=2,
    task="retrieval",
    mode="naive",
)


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [[1.0, 0.0] for _ in items]

    def embed_query(self, text, profile, *, intent):
        return [1.0, 0.0]


def _seed_postgres(pg_engine) -> tuple[str, str]:
    projects = PostgresProjectStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)
    memory = PostgresMemoryStore(pg_engine)

    project = projects.create_project(
        owner_user_id=ANON, name="Anon project", embedding_profile=PROFILE
    )
    thread = threads.create_thread(
        owner_user_id=ANON, title="Anon chat", project_id=None
    )
    memory.add_memory(
        owner_user_id=ANON,
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="nickname",
        value="Anon",
        source=MemorySource.USER_EXPLICIT,
    )
    now = datetime.now(UTC)
    with Session(pg_engine) as session:
        session.add(
            UserSettingsRow(
                owner_user_id=ANON,
                memory_enabled=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            LlmUsageRow(
                owner_user_id=ANON,
                prompt_tokens=5,
                completion_tokens=7,
                total_tokens=12,
                updated_at=now,
            )
        )
        session.commit()
    return project.project_id, thread.thread_id


def test_reassign_moves_everything_the_anonymous_id_owns(pg_engine) -> None:
    project_id, thread_id = _seed_postgres(pg_engine)
    projects = PostgresProjectStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)
    memory = PostgresMemoryStore(pg_engine)

    moved_projects = projects.reassign_owner(
        anonymous_user_id=ANON, verified_user_id=VERIFIED
    )
    moved_threads = threads.reassign_owner(
        anonymous_user_id=ANON, verified_user_id=VERIFIED
    )
    moved_memories = memory.reassign_owner(
        anonymous_user_id=ANON, verified_user_id=VERIFIED
    )

    assert moved_projects == 1
    assert moved_threads == 1
    assert moved_memories == 1

    assert projects.list_projects(owner_user_id=ANON) == []
    assert threads.list_threads(owner_user_id=ANON) == []
    assert memory.list_memories(owner_user_id=ANON) == []

    claimed_projects = projects.list_projects(owner_user_id=VERIFIED)
    assert [p.project_id for p in claimed_projects] == [project_id]
    claimed_threads = threads.list_threads(owner_user_id=VERIFIED)
    assert [t.thread_id for t in claimed_threads] == [thread_id]
    assert [m.key for m in memory.list_memories(owner_user_id=VERIFIED)] == [
        "nickname"
    ]

    # Settings and usage totals follow the account.
    assert memory.get_settings(owner_user_id=VERIFIED).memory_enabled is False
    usage = memory.get_llm_usage(owner_user_id=VERIFIED)
    assert usage.total_tokens == 12


def test_reassign_merges_usage_into_an_existing_verified_account(pg_engine) -> None:
    _seed_postgres(pg_engine)
    memory = PostgresMemoryStore(pg_engine)
    now = datetime.now(UTC)
    with Session(pg_engine) as session:
        session.add(
            UserSettingsRow(
                owner_user_id=VERIFIED,
                memory_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            LlmUsageRow(
                owner_user_id=VERIFIED,
                prompt_tokens=100,
                completion_tokens=1,
                total_tokens=101,
                updated_at=now,
            )
        )
        session.commit()

    memory.reassign_owner(anonymous_user_id=ANON, verified_user_id=VERIFIED)

    # The verified account's own settings win; usage totals add up.
    assert memory.get_settings(owner_user_id=VERIFIED).memory_enabled is True
    assert memory.get_llm_usage(owner_user_id=VERIFIED).total_tokens == 113
    with Session(pg_engine) as session:
        leftover = session.exec(
            select(UserSettingsRow).where(UserSettingsRow.owner_user_id == ANON)
        ).all()
        assert leftover == []


def test_qdrant_reassign_owner_moves_the_tenant_payload() -> None:
    qdrant = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(qdrant, "rag-test-v1", PROFILE)
    document = Document.from_content(
        content=b"claim evidence",
        owner_user_id=ANON,
        project_id="prj_1",
        source_type=SourceType.DOCX,
        source_uri="upload://claim.docx",
        parse_engine="native",
        parse_version="1",
        title="Claim",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=0,
            type=BlockType.PARAGRAPH,
            text="Article 9 covers claims.",
            confidence=1.0,
            locators=[
                OfficePathLocator(part="word/document.xml", path="/document/body/p[1]")
            ],
        )
    ]
    chunks = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=4, leaf_max_tokens=8),
    )
    index.upsert(chunks.chunks, FakeEmbedder())
    chunk_ids = [chunk.chunk_id for chunk in chunks.chunks]

    index.reassign_owner(anonymous_user_id=ANON, verified_user_id=VERIFIED)

    assert index.fetch_chunks(chunk_ids, owner_user_id=ANON, project_id="prj_1") == {}
    moved = index.fetch_chunks(chunk_ids, owner_user_id=VERIFIED, project_id="prj_1")
    assert sorted(moved.keys()) == sorted(chunk_ids)


def test_claim_endpoint_requires_a_verified_login_and_moves_history(pg_engine) -> None:
    from tests.test_thread_api import _setup

    _seed_postgres(pg_engine)

    from rag_ingestion.api.auth import VerifiedIdentity

    class FakeVerifier:
        def verify_bearer(self, authorization: str) -> VerifiedIdentity:
            token = authorization.partition(" ")[2]
            if token != "good-token":
                raise ValueError("bad token")
            return VerifiedIdentity(uid=VERIFIED)

    from rag_ingestion.memory.store import PostgresMemoryStore as _MemoryStore

    client, _, _qdrant, _bundle = _setup(
        pg_engine, auth_verifier=FakeVerifier(), memory=_MemoryStore(pg_engine)
    )

    # No Authorization header: the fallback lets normal routes through, but
    # claiming without a verified login is refused.
    refused = client.post(
        "/auth/claim",
        json={"anonymous_user_id": ANON},
        headers={"X-User-ID": ANON},
    )
    assert refused.status_code == 401

    claimed = client.post(
        "/auth/claim",
        json={"anonymous_user_id": ANON},
        headers={"Authorization": "Bearer good-token", "X-User-ID": VERIFIED},
    )
    assert claimed.status_code == 200
    body = claimed.json()
    assert body["projects"] == 1
    assert body["threads"] == 1
    assert body["memories"] == 1

    listed = client.get(
        "/threads",
        headers={"Authorization": "Bearer good-token", "X-User-ID": VERIFIED},
    ).json()
    assert [t["title"] for t in listed] == ["Anon chat"]
    assert client.get("/threads", headers={"X-User-ID": ANON}).json() == []
    # The verified uid is not reachable through the anonymous fallback.
    assert (
        client.get("/threads", headers={"X-User-ID": VERIFIED}).status_code == 401
    )
