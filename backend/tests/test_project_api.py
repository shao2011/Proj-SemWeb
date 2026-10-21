from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from rag_ingestion.retrieval.answering import MockAnswerer
from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.models import Block, BlockType, Document, OfficePathLocator, SourceType
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.api.routes import create_query_app
from rag_ingestion.retrieval.reranking import RankedResult


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [self._vector(item) for item in items]

    def embed_query(self, text, profile, *, intent):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float("travel" in text.casefold()), float("security" in text.casefold())]


class ReverseReranker:
    def __init__(self) -> None:
        self.candidate_count = 0

    def rerank(self, query, documents, *, top_n):
        self.candidate_count = len(documents)
        return [
            RankedResult(index=index, relevance_score=1.0 - index / 10)
            for index in reversed(range(min(top_n, len(documents))))
        ]


def _setup(pg_engine, *, media_store=None, retrieval_cache=None):
    qdrant = QdrantClient(location=":memory:")
    store = PostgresProjectStore(pg_engine)
    profile = EmbeddingProfile(
        provider="cohere",
        model="fake-2d",
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )
    reranker = ReverseReranker()
    bundle = ProviderBundle(
        provider=Provider.COHERE,
        profile=profile,
        collection_name="rag-cohere-v1",
        embedder=FakeEmbedder(),
        reranker=reranker,
    )
    registry = ProviderRegistry([bundle])
    app = create_query_app(
        qdrant,
        registry,
        store,
        answerer=MockAnswerer(),
        media_store=media_store,
        retrieval_cache=retrieval_cache,
    )
    return TestClient(app), qdrant, store, bundle, reranker


def _index_evidence(qdrant, bundle, *, owner_user_id: str, project_id: str) -> None:
    document = Document.from_content(
        content=b"project evidence",
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title="Policy",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=index,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[
                OfficePathLocator(
                    part="word/document.xml", path=f"/document/body/p[{index + 1}]"
                )
            ],
        )
        for index, text in enumerate(
            [
                "Travel policy uses ZX-42.",
                "Travel receipts are required.",
                "Travel approval is mandatory.",
            ]
        )
    ]
    chunks = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=4, leaf_max_tokens=8),
    )
    QdrantHybridIndex(qdrant, bundle.collection_name, bundle.profile).upsert(
        chunks.chunks, bundle.embedder
    )


def test_project_creation_pins_provider_and_lists_only_owner_projects(pg_engine) -> None:
    client, _, store, _, _ = _setup(pg_engine)
    try:
        created = client.post(
            "/projects",
            headers={"X-User-ID": "user-a"},
            json={"name": "Private research", "provider": "cohere"},
        )
        assert created.status_code == 201
        project = created.json()
        assert project["owner_user_id"] == "user-a"
        assert project["embedding_profile"]["provider"] == "cohere"
        assert client.get("/projects", headers={"X-User-ID": "user-a"}).json() == [project]
        assert client.get("/projects", headers={"X-User-ID": "user-b"}).json() == []
    finally:
        store.close()


def test_project_creation_rejects_invalid_provider_with_422(pg_engine) -> None:
    client, _, store, _, _ = _setup(pg_engine)
    try:
        response = client.post(
            "/projects",
            headers={"X-User-ID": "user-a"},
            json={"name": "Invalid", "provider": "unknown"},
        )
        assert response.status_code == 422
    finally:
        store.close()


def test_project_can_be_renamed(pg_engine) -> None:
    client, _, store, _, _ = _setup(pg_engine)
    try:
        created = client.post(
            "/projects",
            headers={"X-User-ID": "user-a"},
            json={"name": "Original", "provider": "cohere"},
        )
        project_id = created.json()["project_id"]

        assert (
            client.patch(
                f"/projects/{project_id}",
                headers={"X-User-ID": "user-b"},
                json={"name": "Hijacked"},
            ).status_code
            == 404
        )

        response = client.patch(
            f"/projects/{project_id}",
            headers={"X-User-ID": "user-a"},
            json={"name": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

        assert (
            client.get("/projects", headers={"X-User-ID": "user-a"}).json()[0]["name"]
            == "Renamed"
        )

        blank = client.patch(
            f"/projects/{project_id}",
            headers={"X-User-ID": "user-a"},
            json={"name": "   "},
        )
        assert blank.status_code == 422
    finally:
        store.close()


def test_project_can_be_deleted(pg_engine) -> None:
    client, _, store, _, _ = _setup(pg_engine)
    try:
        created = client.post(
            "/projects",
            headers={"X-User-ID": "user-a"},
            json={"name": "Disposable", "provider": "cohere"},
        )
        project_id = created.json()["project_id"]

        assert (
            client.delete(
                f"/projects/{project_id}", headers={"X-User-ID": "user-b"}
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/projects/{project_id}", headers={"X-User-ID": "user-a"}
            ).status_code
            == 204
        )
        assert client.get("/projects", headers={"X-User-ID": "user-a"}).json() == []
        assert (
            client.delete(
                f"/projects/{project_id}", headers={"X-User-ID": "user-a"}
            ).status_code
            == 404
        )
    finally:
        store.close()


def test_foreign_project_returns_404_before_qdrant_access(pg_engine) -> None:
    client, qdrant, store, bundle, _ = _setup(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Private", embedding_profile=bundle.profile
    )
    qdrant.collection_exists = Mock(wraps=qdrant.collection_exists)
    try:
        response = client.post(
            f"/projects/{project.project_id}/query",
            headers={"X-User-ID": "user-b"},
            json={"query": "travel"},
        )
        assert response.status_code == 404
        qdrant.collection_exists.assert_not_called()
    finally:
        store.close()


def test_query_expands_candidates_then_returns_reranker_order(pg_engine) -> None:
    client, qdrant, store, bundle, reranker = _setup(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Evidence", embedding_profile=bundle.profile
    )
    _index_evidence(
        qdrant, bundle, owner_user_id="user-a", project_id=project.project_id
    )
    try:
        response = client.post(
            f"/projects/{project.project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "travel", "limit": 2},
        )
        assert response.status_code == 200
        hits = response.json()["hits"]
        assert len(hits) == 2
        assert reranker.candidate_count > 2
        assert [hit["score"] for hit in hits] == [0.9, 1.0]
    finally:
        store.close()


def test_answer_uses_reranked_project_evidence_as_citations(pg_engine) -> None:
    client, qdrant, store, bundle, _ = _setup(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Evidence", embedding_profile=bundle.profile
    )
    _index_evidence(
        qdrant, bundle, owner_user_id="user-a", project_id=project.project_id
    )
    try:
        response = client.post(
            f"/projects/{project.project_id}/answer",
            headers={"X-User-ID": "user-a"},
            json={"query": "travel", "limit": 2},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["citations"] == [hit["chunk_id"] for hit in body["hits"]]
        # Two sibling leaves recalled -> auto-merge hands the LLM ONE contiguous
        # section, not two fragments; citations above stay leaf-level.
        assert body["answer"].startswith("Retrieved 1 evidence chunks")
        for hit in body["hits"]:
            assert hit["source_text"] in body["answer"]
    finally:
        store.close()


def test_answer_returns_explicit_no_evidence_for_empty_project(pg_engine) -> None:
    client, qdrant, store, bundle, _ = _setup(pg_engine)
    target = store.create_project(
        owner_user_id="user-a", name="Empty", embedding_profile=bundle.profile
    )
    other = store.create_project(
        owner_user_id="user-a", name="Other", embedding_profile=bundle.profile
    )
    _index_evidence(
        qdrant, bundle, owner_user_id="user-a", project_id=other.project_id
    )
    try:
        response = client.post(
            f"/projects/{target.project_id}/answer",
            headers={"X-User-ID": "user-a"},
            json={"query": "travel", "limit": 2},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["hits"] == []
        assert body["citations"] == []
        assert "no evidence" in body["answer"].casefold()
    finally:
        store.close()


def test_delete_project_purges_vectors_media_and_cache(pg_engine, tmp_path) -> None:
    from rag_ingestion.storage.media import LocalMediaStore
    from rag_ingestion.storage.projects import FileProcessingStatus

    media_store = LocalMediaStore(tmp_path)
    cache = Mock()
    client, qdrant, store, bundle, _ = _setup(
        pg_engine, media_store=media_store, retrieval_cache=cache
    )
    try:
        created = client.post(
            "/projects",
            headers={"X-User-ID": "user-a"},
            json={"name": "Doomed", "provider": "cohere"},
        )
        project_id = created.json()["project_id"]
        _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
        _index_evidence(
            qdrant, bundle, owner_user_id="user-a", project_id="project-kept"
        )

        record = store.add_file(
            owner_user_id="user-a",
            project_id=project_id,
            filename="policy.docx",
            content_hash="hash-1",
        )
        store.update_file_status(
            owner_user_id="user-a",
            project_id=project_id,
            file_id=record.file_id,
            status=FileProcessingStatus.PROCESSING,
        )
        store.update_file_status(
            owner_user_id="user-a",
            project_id=project_id,
            file_id=record.file_id,
            status=FileProcessingStatus.COMPLETED,
            doc_id="doc_media_test",
        )
        media_store.put(
            "doc_media_test/page/aa.png", b"img", content_type="image/png"
        )
        media_store.put(
            "doc_media_test/markdown/bb.md", b"md", content_type="text/markdown"
        )

        response = client.delete(
            f"/projects/{project_id}", headers={"X-User-ID": "user-a"}
        )
        assert response.status_code == 204

        records, _ = qdrant.scroll(
            bundle.collection_name, limit=50, with_payload=True
        )
        remaining = {record.payload["project_id"] for record in records}
        assert project_id not in remaining, "deleted project's vectors must be gone"
        assert "project-kept" in remaining, "other projects must keep their vectors"

        assert not media_store.exists("doc_media_test/page/aa.png")
        assert not media_store.exists("doc_media_test/markdown/bb.md")
        cache.invalidate_project.assert_called_once_with(project_id)
    finally:
        store.close()
