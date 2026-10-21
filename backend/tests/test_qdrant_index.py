from qdrant_client import QdrantClient, models

from rag_ingestion.chunking import ChunkLevel, ChunkingConfig, chunk_document
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    OfficePathLocator,
    SourceType,
)
from rag_ingestion.indexing.qdrant_index import (
    EmbeddingProfile,
    QdrantHybridIndex,
)


class FakeEmbedder:
    def embed_documents(
        self, items: list[str], profile: EmbeddingProfile, *, intent: str
    ) -> list[list[float]]:
        assert intent == "document"
        return [self._vector(item) for item in items]

    def embed_query(
        self, text: str, profile: EmbeddingProfile, *, intent: str
    ) -> list[float]:
        assert intent == "query"
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.casefold()
        return [float("travel" in normalized), float("security" in normalized)]


def _chunks(*, owner_user_id: str = "user-a", project_id: str = "project-a"):
    document = Document.from_content(
        content=b"hybrid index fixture",
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title="Policy handbook",
        domain="legal",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=0,
            type=BlockType.PARAGRAPH,
            text="Travel requests require code ZX-42.",
            confidence=0.98,
            language="en",
            section_path=["Travel"],
            locators=[
                OfficePathLocator(part="word/document.xml", path="/document/body/p[1]")
            ],
        ),
        Block.from_source(
            doc_id=document.doc_id,
            order=1,
            type=BlockType.PARAGRAPH,
            text="Security incidents use code SEC-9.",
            confidence=0.95,
            language="en",
            section_path=["Security"],
            locators=[
                OfficePathLocator(part="word/document.xml", path="/document/body/p[2]")
            ],
        ),
    ]
    result = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=64, leaf_max_tokens=128),
    )
    return [chunk for chunk in result.chunks if chunk.level is ChunkLevel.LEAF]


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="fake",
        model="deterministic-2d",
        version="1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )


def test_upsert_creates_named_vectors_and_preserves_provenance_payload() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    chunks = _chunks()

    index.upsert(chunks, FakeEmbedder())

    collection = client.get_collection("policy-v1")
    assert set(collection.config.params.vectors) == {"dense"}
    assert set(collection.config.params.sparse_vectors or {}) == {"sparse"}
    assert collection.config.metadata == {"embedding_profile": _profile().model_dump(mode="json")}

    records, _ = client.scroll(
        "policy-v1", limit=10, with_payload=True, with_vectors=True
    )
    assert len(records) == 2
    point = next(record for record in records if record.payload["section_path"] == ["Travel"])
    assert set(point.vector) == {"dense", "sparse"}
    assert point.payload["chunk_id"] == chunks[0].chunk_id
    assert point.payload["source_block_ids"] == chunks[0].source_block_ids
    assert point.payload["locators"] == [
        locator.model_dump(mode="json") for locator in chunks[0].locators
    ]
    assert point.payload["confidence"] == 0.98
    assert point.payload["citable"] is True
    assert point.payload["embedding_provider"] == "fake"
    assert point.payload["embedding_model"] == "deterministic-2d"
    assert point.payload["embedding_version"] == "1"
    assert point.payload["embedding_mode"] == "naive"


def test_every_field_a_filter_names_is_indexed(monkeypatch) -> None:
    """A managed cluster runs strict mode, where filtering an unindexed payload
    field is a 400 rather than a slow scan. `citable` is filtered on every
    citation-bearing search and is a bool, not a keyword like the rest.

    Asserted on the requests rather than on `payload_schema`, which the
    in-memory client does not populate.
    """
    client = QdrantClient(location=":memory:")
    requested: dict[str, object] = {}
    create_index = client.create_payload_index

    def spy(*, collection_name: str, field_name: str, field_schema, wait: bool):
        requested[field_name] = field_schema
        return create_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
            wait=wait,
        )

    monkeypatch.setattr(client, "create_payload_index", spy)

    QdrantHybridIndex(client, "policy-v1", _profile()).ensure_collection()

    keyword = models.PayloadSchemaType.KEYWORD
    assert requested == {
        "owner_user_id": keyword,
        "project_id": keyword,
        "level": keyword,
        "chunk_id": keyword,
        "doc_id": keyword,
        "source_block_ids": keyword,
        "citable": models.PayloadSchemaType.BOOL,
    }


def test_hybrid_search_uses_sparse_identifier_and_owner_project_filters() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    index.upsert(_chunks(), FakeEmbedder())
    index.upsert(
        _chunks(owner_user_id="user-b", project_id="project-b"), FakeEmbedder()
    )

    hits = index.hybrid_search(
        "ZX-42",
        FakeEmbedder(),
        owner_user_id="user-a",
        project_id="project-a",
        language="en",
        limit=2,
    )

    assert hits
    assert all(hit.payload["owner_user_id"] == "user-a" for hit in hits)
    assert all(hit.payload["project_id"] == "project-a" for hit in hits)
    assert hits[0].payload["section_path"] == ["Travel"]


def test_collection_rejects_a_different_embedding_profile() -> None:
    client = QdrantClient(location=":memory:")
    QdrantHybridIndex(client, "policy-v1", _profile()).ensure_collection()
    incompatible = _profile().model_copy(update={"version": "2"})

    index = QdrantHybridIndex(client, "policy-v1", incompatible)

    try:
        index.ensure_collection()
    except ValueError as error:
        assert "embedding profile" in str(error).lower()
    else:
        raise AssertionError("Expected an incompatible embedding profile to be rejected")


def test_fetch_chunks_returns_the_requested_chunks() -> None:
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "fetch-v1", _profile()
    )
    chunks = _chunks()
    index.upsert(chunks, FakeEmbedder())

    loaded = index.fetch_chunks(
        [chunk.chunk_id for chunk in chunks],
        owner_user_id="user-a",
        project_id="project-a",
    )

    assert set(loaded) == {chunk.chunk_id for chunk in chunks}
    assert loaded[chunks[0].chunk_id].source_text == chunks[0].source_text


def test_fetch_chunks_never_crosses_a_tenant_boundary() -> None:
    """The guard that used to live in the Qdrant filter.

    Fetching by point id takes no filter, so the ownership check moved into
    Python. If it is ever dropped, a caller who knows a chunk id reads another
    tenant's text, and nothing else in the stack would notice.
    """
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "fetch-v1", _profile()
    )
    mine = _chunks(owner_user_id="user-a", project_id="project-a")
    theirs = _chunks(owner_user_id="user-b", project_id="project-b")
    index.upsert(mine, FakeEmbedder())
    index.upsert(theirs, FakeEmbedder())

    wanted = [chunk.chunk_id for chunk in mine + theirs]

    as_user_a = index.fetch_chunks(
        wanted, owner_user_id="user-a", project_id="project-a"
    )
    assert set(as_user_a) == {chunk.chunk_id for chunk in mine}

    other_project = index.fetch_chunks(
        wanted, owner_user_id="user-a", project_id="project-b"
    )
    assert other_project == {}, "owner alone must not unlock another project"


def test_fetch_chunks_tolerates_unknown_ids() -> None:
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "fetch-v1", _profile()
    )
    chunks = _chunks()
    index.upsert(chunks, FakeEmbedder())

    loaded = index.fetch_chunks(
        [chunks[0].chunk_id, "chk_does_not_exist"],
        owner_user_id="user-a",
        project_id="project-a",
    )

    assert set(loaded) == {chunks[0].chunk_id}


def test_delete_by_project_removes_only_that_tenant() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    index.upsert(_chunks(), FakeEmbedder())
    index.upsert(
        _chunks(owner_user_id="user-b", project_id="project-a"), FakeEmbedder()
    )
    index.upsert(
        _chunks(owner_user_id="user-a", project_id="project-b"), FakeEmbedder()
    )

    index.delete_by_project(owner_user_id="user-a", project_id="project-a")

    records, _ = client.scroll("policy-v1", limit=50, with_payload=True)
    tenants = {
        (record.payload["owner_user_id"], record.payload["project_id"])
        for record in records
    }
    assert ("user-a", "project-a") not in tenants
    assert ("user-b", "project-a") in tenants
    assert ("user-a", "project-b") in tenants

    index.delete_by_project(owner_user_id="user-a", project_id="project-a")
    survivors, _ = client.scroll("policy-v1", limit=50, with_payload=False)
    assert len(survivors) == len(records), "re-delete must be a no-op"


def test_delete_by_project_on_missing_collection_creates_nothing() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "never-created", _profile())

    index.delete_by_project(owner_user_id="user-a", project_id="project-a")

    assert not client.collection_exists("never-created")


def test_delete_by_doc_removes_only_that_document() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    first = _chunks()
    index.upsert(first, FakeEmbedder())
    other_project = _chunks(owner_user_id="user-a", project_id="project-b")
    index.upsert(other_project, FakeEmbedder())

    target_doc = first[0].doc_id
    index.delete_by_doc(
        owner_user_id="user-a", project_id="project-a", doc_id=target_doc
    )

    records, _ = client.scroll("policy-v1", limit=50, with_payload=True)
    remaining_docs = {record.payload["doc_id"] for record in records}
    assert target_doc not in remaining_docs
    assert {record.payload["project_id"] for record in records} == {"project-b"}


def test_hybrid_search_excludes_the_named_documents() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    chunks = _chunks()
    index.upsert(chunks, FakeEmbedder())

    included = index.hybrid_search(
        "ZX-42",
        FakeEmbedder(),
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
    )
    assert included

    excluded = index.hybrid_search(
        "ZX-42",
        FakeEmbedder(),
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        exclude_doc_ids=[chunks[0].doc_id],
    )
    assert excluded == []


def test_fetch_by_block_ids_excludes_the_named_documents() -> None:
    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    chunks = _chunks()
    index.upsert(chunks, FakeEmbedder())
    block_ids = chunks[0].source_block_ids

    assert index.fetch_by_block_ids(
        block_ids, owner_user_id="user-a", project_id="project-a"
    )
    assert (
        index.fetch_by_block_ids(
            block_ids,
            owner_user_id="user-a",
            project_id="project-a",
            exclude_doc_ids=[chunks[0].doc_id],
        )
        == []
    )


def test_document_sparse_values_saturate_instead_of_growing_linearly() -> None:
    from rag_ingestion.indexing.qdrant_index import _sparse_vector, _tokens

    once = _sparse_vector("thuế cơ bản", None, intent="document")
    many = _sparse_vector("thuế thuế thuế thuế thuế thuế thuế thuế thuế thuế", None, intent="document")

    def value_of(vector, token: str) -> float:
        import hashlib

        index = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")
        return dict(zip(vector.indices, vector.values))[index]

    single = value_of(once, "thuế")
    repeated = value_of(many, "thuế")
    assert repeated > single, "more occurrences must still score higher"
    assert repeated < 10 * single, "BM25 saturates; raw linear TF must be gone"
    # k1=1.2 caps the term factor at k1+1: the asymptote of BM25 saturation.
    assert repeated < 2.2


def test_document_sparse_values_penalize_long_chunks() -> None:
    from rag_ingestion.indexing.qdrant_index import _sparse_vector

    import hashlib

    index = int.from_bytes(hashlib.blake2b("thuế".encode(), digest_size=4).digest(), "big")
    short = _sparse_vector("thuế cơ bản", None, intent="document")
    long = _sparse_vector("thuế " + "từ " * 400, None, intent="document")

    short_value = dict(zip(short.indices, short.values))[index]
    long_value = dict(zip(long.indices, long.values))[index]
    assert short_value > long_value, (
        "same term count in a much longer chunk must weigh less (length norm)"
    )


def test_query_sparse_values_stay_raw_counts() -> None:
    from rag_ingestion.indexing.qdrant_index import _sparse_vector

    vector = _sparse_vector("thuế thuế thuế", None, intent="query")
    assert vector.values == [3.0]


def test_thai_text_tokenizes_into_grams_not_one_blob() -> None:
    from rag_ingestion.indexing.qdrant_index import _sparse_vector, _tokens

    tokens = _tokens("ภาษีมูลค่าเพิ่มสำหรับสินค้านำเข้า", "th")
    assert len(tokens) > 3, "an unsegmented Thai run must not stay one token"

    document = _sparse_vector(
        "ภาษีมูลค่าเพิ่มสำหรับสินค้านำเข้า", "th", intent="document"
    )
    query = _sparse_vector("ภาษี", "th", intent="query")
    assert set(document.indices) & set(query.indices), (
        "a Thai query word must share sparse dimensions with the document"
    )


def test_thai_tokens_include_dictionary_words_alongside_grams() -> None:
    from rag_ingestion.indexing.qdrant_index import _tokens

    tokens = _tokens("ผู้ประกอบการต้องยื่นแบบแสดงรายการภาษี", "th")
    assert "ภาษี" in tokens, "segmented Thai words must be searchable as words"
    assert any(len(token) == 2 for token in tokens), (
        "char bigrams stay as the out-of-dictionary fallback"
    )


def test_search_path_never_creates_collections_or_indexes() -> None:
    from unittest.mock import Mock

    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    index.upsert(_chunks(), FakeEmbedder())

    client.create_collection = Mock(wraps=client.create_collection)
    client.create_payload_index = Mock(wraps=client.create_payload_index)
    client.get_collection = Mock(wraps=client.get_collection)

    hits = index.hybrid_search(
        "ZX-42",
        FakeEmbedder(),
        owner_user_id="user-a",
        project_id="project-a",
        limit=2,
    )

    assert hits, "sanity: the search itself still works"
    client.create_collection.assert_not_called()
    client.create_payload_index.assert_not_called()
    client.get_collection.assert_not_called()


def test_ensure_collection_is_memoized_per_index_instance() -> None:
    from unittest.mock import Mock

    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    index.upsert(_chunks(), FakeEmbedder())

    client.create_payload_index = Mock(wraps=client.create_payload_index)
    index.upsert(_chunks(project_id="project-b"), FakeEmbedder())

    client.create_payload_index.assert_not_called()


def test_hybrid_search_accepts_a_precomputed_dense_vector() -> None:
    from unittest.mock import Mock

    client = QdrantClient(location=":memory:")
    index = QdrantHybridIndex(client, "policy-v1", _profile())
    index.upsert(_chunks(), FakeEmbedder())

    embedder = FakeEmbedder()
    embedder.embed_query = Mock(side_effect=embedder.embed_query)
    hits = index.hybrid_search(
        "ZX-42",
        embedder,
        owner_user_id="user-a",
        project_id="project-a",
        limit=2,
        dense_vector=[1.0, 0.0],
    )

    assert hits
    embedder.embed_query.assert_not_called()
