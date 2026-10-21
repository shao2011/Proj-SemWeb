from pathlib import Path

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

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
        normalized = text.casefold()
        return [float("travel" in normalized), float("security" in normalized)]


class IdentityReranker:
    def rerank(self, query, documents, *, top_n):
        return [
            RankedResult(index=index, relevance_score=1.0 - index / 10)
            for index in range(min(top_n, len(documents)))
        ]


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="cohere",
        model="deterministic-2d",
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )


def _bundle(collection_name: str = "policy-v1") -> ProviderBundle:
    return ProviderBundle(
        provider=Provider.COHERE,
        profile=_profile(),
        collection_name=collection_name,
        embedder=FakeEmbedder(),
        reranker=IdentityReranker(),
    )


def _client(pg_engine) -> tuple[TestClient, PostgresProjectStore, str]:
    qdrant = QdrantClient(location=":memory:")
    store = PostgresProjectStore(pg_engine)
    bundle = _bundle()
    project = store.create_project(
        owner_user_id="user-a", name="Policy", embedding_profile=bundle.profile
    )
    document = Document.from_content(
        content=b"query API fixture",
        owner_user_id="user-a",
        project_id=project.project_id,
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
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=64, leaf_max_tokens=128),
    )
    QdrantHybridIndex(qdrant, bundle.collection_name, bundle.profile).upsert(
        chunks.chunks, bundle.embedder
    )
    app = create_query_app(qdrant, ProviderRegistry([bundle]), store)
    return TestClient(app), store, project.project_id


def test_query_returns_ranked_citable_evidence_with_provenance(pg_engine) -> None:
    client, store, project_id = _client(pg_engine)
    try:
        response = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42", "language": "en", "limit": 5},
        )
        assert response.status_code == 200
        hit = response.json()["hits"][0]
        assert hit["source_text"] == "Travel requests require code ZX-42."
        assert hit["section_path"] == ["Travel"]
        assert hit["citable"] is True
        assert hit["locators"] == [
            {
                "kind": "office_path",
                "part": "word/document.xml",
                "path": "/document/body/p[1]",
            }
        ]
    finally:
        store.close()


def test_query_does_not_create_a_missing_collection(pg_engine) -> None:
    qdrant = QdrantClient(location=":memory:")
    store = PostgresProjectStore(pg_engine)
    bundle = _bundle("missing-v1")
    project = store.create_project(
        owner_user_id="user-a", name="Missing", embedding_profile=bundle.profile
    )
    client = TestClient(create_query_app(qdrant, ProviderRegistry([bundle]), store))
    try:
        response = client.post(
            f"/projects/{project.project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "travel"},
        )
        # An empty result, not a fault: a project whose provider has nothing
        # indexed yet has no hits to give, and answering falls back to plain chat.
        assert response.status_code == 200
        assert response.json()["hits"] == []
        # The point of the guard: reading must not create the collection.
        assert qdrant.collection_exists("missing-v1") is False
    finally:
        store.close()


def test_query_rejects_invalid_input_and_user_header(pg_engine) -> None:
    client, store, project_id = _client(pg_engine)
    try:
        blank = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "   "},
        )
        excessive = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "travel", "limit": 101},
        )
        missing_user = client.post(
            f"/projects/{project_id}/query", json={"query": "travel"}
        )
        blank_user = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "   "},
            json={"query": "travel"},
        )
        assert {blank.status_code, excessive.status_code, missing_user.status_code, blank_user.status_code} == {422}
    finally:
        store.close()


class _SharedRedis:
    """One keyspace for get/setex/incr, as Redis has."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        return True

    def incr(self, key: str) -> int:
        current = int(self.store.get(key, 0)) + 1
        self.store[key] = str(current)
        return current


class _CountingReranker:
    """The reranker is the expensive call, so it is the honest witness for
    whether a cache hit really skipped the work."""

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, query, documents, *, top_n):
        self.calls += 1
        return [
            RankedResult(index=index, relevance_score=1.0)
            for index in range(min(top_n, len(documents)))
        ]


def _cached_client(pg_engine, reranker, cache):
    qdrant = QdrantClient(location=":memory:")
    store = PostgresProjectStore(pg_engine)
    bundle = ProviderBundle(
        provider=Provider.COHERE,
        profile=_profile(),
        collection_name="policy-v1",
        embedder=FakeEmbedder(),
        reranker=reranker,
    )
    project = store.create_project(
        owner_user_id="user-a", name="Policy", embedding_profile=bundle.profile
    )
    document = Document.from_content(
        content=b"cache fixture",
        owner_user_id="user-a",
        project_id=project.project_id,
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title="Policy handbook",
    )
    block = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.PARAGRAPH,
        text="Travel requests require code ZX-42.",
        confidence=0.98,
        locators=[OfficePathLocator(part="word/document.xml", path="/document/body/p[1]")],
    )
    chunks = chunk_document(
        document,
        [block],
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=64, leaf_max_tokens=128),
    )
    QdrantHybridIndex(qdrant, bundle.collection_name, bundle.profile).upsert(
        chunks.chunks, bundle.embedder
    )
    app = create_query_app(
        qdrant, ProviderRegistry([bundle]), store, retrieval_cache=cache
    )
    return TestClient(app), store, project.project_id


def test_a_repeated_query_skips_the_reranker_entirely(pg_engine) -> None:
    from rag_ingestion.retrieval.cache import RetrievalCache

    reranker = _CountingReranker()
    client, store, project_id = _cached_client(
        pg_engine, reranker, RetrievalCache(_SharedRedis(), ttl_seconds=300)
    )
    try:
        body = {"query": "travel code", "limit": 5}
        first = client.post(
            f"/projects/{project_id}/query", json=body, headers={"X-User-ID": "user-a"}
        )
        second = client.post(
            f"/projects/{project_id}/query", json=body, headers={"X-User-ID": "user-a"}
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert reranker.calls == 1, "the second query must not reach the reranker"
        assert first.json()["hits"] == second.json()["hits"], (
            "a cached answer must be identical to the one it replaces"
        )
    finally:
        store.close()


def test_without_the_cache_every_query_pays_the_reranker(pg_engine) -> None:
    """Negative control for the test above."""
    reranker = _CountingReranker()
    client, store, project_id = _cached_client(pg_engine, reranker, None)
    try:
        body = {"query": "travel code", "limit": 5}
        for _ in range(2):
            client.post(
                f"/projects/{project_id}/query", json=body, headers={"X-User-ID": "user-a"}
            )

        assert reranker.calls == 2
    finally:
        store.close()


def test_a_different_question_is_not_served_from_cache(pg_engine) -> None:
    from rag_ingestion.retrieval.cache import RetrievalCache

    reranker = _CountingReranker()
    client, store, project_id = _cached_client(
        pg_engine, reranker, RetrievalCache(_SharedRedis(), ttl_seconds=300)
    )
    try:
        for query in ("travel code", "something else entirely"):
            client.post(
                f"/projects/{project_id}/query",
                json={"query": query, "limit": 5},
                headers={"X-User-ID": "user-a"},
            )

        assert reranker.calls == 2, "each distinct question must be computed"
    finally:
        store.close()
