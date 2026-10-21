"""Which references survive `reference_cap`.

Expansion pulls in the chunks that selected chunks point at, then keeps only
`reference_cap` of them. It sorted that list by `scores.get(chunk.chunk_id, 0.0)`,
and an expanded chunk is by definition one the reranker never saw, so every key
missed and every candidate scored 0.0: the sort was a no-op and the cap kept
whatever order Qdrant scrolled back, which is by point id.

The order now comes from how strongly each referenced block was pointed at.
"""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

from rag_ingestion.chunking import Chunk, ChunkLevel, ChunkingConfig, chunk_document
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    OfficePathLocator,
    Relation,
    RelationType,
    SourceType,
)
from rag_ingestion.retrieval import RetrievalConfig, run_retrieval
from rag_ingestion.retrieval.chain import _selected_score
from rag_ingestion.retrieval.reranking import RankedResult

_PROFILE = EmbeddingProfile(
    provider="cohere",
    model="fake-3d",
    version="v1",
    dimensions=3,
    task="retrieval",
    mode="naive",
)


class _Embedder:
    def embed_documents(self, items, profile, *, intent):
        return [self._vector(item) for item in items]

    def embed_query(self, text, profile, *, intent):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        return [
            float("thuế" in lowered),
            float("phạt" in lowered),
            float("điều" in lowered),
        ]


class _ScoreByText:
    """Ranks by a caller-supplied score per text fragment.

    An identity reranker cannot express "this chunk matters more than that one",
    which is exactly what the ordering under test reads.
    """

    def __init__(self, weights: dict[str, float]) -> None:
        self._weights = weights

    def rerank(self, query, documents, *, top_n):
        scored = [
            (index, self._score(text)) for index, text in enumerate(documents)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [
            RankedResult(index=index, relevance_score=score)
            for index, score in scored[:top_n]
        ]

    def _score(self, text: str) -> float:
        for fragment, weight in self._weights.items():
            if fragment in text:
                return weight
        return 0.0


def _index(blocks: list[Block], document: Document) -> QdrantHybridIndex:
    chunked = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(
            leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40
        ),
    )
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-reference-v1", _PROFILE
    )
    index.upsert(chunked.chunks, _Embedder())
    return index


def _document() -> Document:
    return Document.from_content(
        content=b"reference ordering fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://relations.docx",
        parse_engine="native",
        parse_version="1",
        title="Tài liệu",
    )


def _block(document: Document, order: int, text: str, *, target=None) -> Block:
    return Block.from_source(
        doc_id=document.doc_id,
        order=order,
        type=BlockType.PARAGRAPH,
        text=text,
        confidence=1.0,
        locators=[
            OfficePathLocator(part="word/document.xml", path=f"/p[{order + 1}]")
        ],
        relations=(
            [Relation(type=RelationType.CAPTION_OF, target_block_id=target)]
            if target
            else []
        ),
    )


def test_the_cap_keeps_the_reference_of_the_stronger_chunk() -> None:
    """Two competing references, room for one.

    Both source chunks match the query, one much better. With the cap at one, the
    reference belonging to the better-scoring chunk is the one that has to
    survive. Under the old sort both scored 0.0 and the winner came down to the
    UUID5 of the chunk id.
    """
    document = _document()
    weak_target = _block(document, 0, "Bảng số liệu thu ngân sách theo quý.")
    strong_target = _block(document, 1, "Danh mục hồ sơ lưu trữ nội bộ.")
    weak_source = _block(
        document, 2, "Điều 9 nói về thuế suất.", target=weak_target.block_id
    )
    strong_source = _block(
        document, 3, "Điều 5 quy định thuế và phạt.", target=strong_target.block_id
    )
    index = _index(
        [weak_target, strong_target, weak_source, strong_source], document
    )

    outcome = run_retrieval(
        index,
        _Embedder(),
        _ScoreByText({"Điều 5": 0.95, "Điều 9": 0.10}),
        "thuế",
        owner_user_id="user-a",
        project_id="project-a",
        limit=2,
        config=RetrievalConfig(reference_cap=1, route_limit=1, global_k=10),
    )

    texts = " ".join(chunk.source_text for chunk in outcome.chunks)
    assert outcome.diagnostics["reference_expanded"] == 1
    assert "Danh mục hồ sơ" in texts, texts
    assert "Bảng số liệu" not in texts, texts


def test_selected_score_resolves_a_merged_ancestor_to_its_best_leaf() -> None:
    """Where the 0.0 came from.

    Auto-Merge replaces ranked leaves with an ancestor, and an ancestor was never
    in the pool the reranker scored, so reading `scores[ancestor]` found nothing.
    Its strongest leaf is the relevance it is standing on.
    """
    leaf_low = Chunk(
        chunk_id="chunk_leaf_low",
        doc_id="doc_1",
        owner_user_id="user-a",
        project_id="project-a",
        level=ChunkLevel.LEAF,
        source_block_ids=["block_1"],
        source_text="low",
        index_text="low",
        locators=[OfficePathLocator(part="word/document.xml", path="/p[1]")],
        confidence=1.0,
        citable=True,
        language="vi",
    )
    leaf_high = leaf_low.model_copy(
        update={"chunk_id": "chunk_leaf_high", "source_block_ids": ["block_2"]}
    )
    ancestor = leaf_low.model_copy(
        update={
            "chunk_id": "chunk_ancestor",
            "level": ChunkLevel.SECTION,
            "child_ids": [leaf_low.chunk_id, leaf_high.chunk_id],
        }
    )
    scores = {leaf_low.chunk_id: 0.2, leaf_high.chunk_id: 0.8}
    chunks_by_id = {
        chunk.chunk_id: chunk for chunk in (leaf_low, leaf_high, ancestor)
    }

    assert _selected_score(ancestor, scores, chunks_by_id) == pytest.approx(0.8)
    # A leaf with a score of its own keeps it.
    assert _selected_score(leaf_low, scores, chunks_by_id) == pytest.approx(0.2)
    # Nothing known: 0.0, not a KeyError.
    unknown = leaf_low.model_copy(update={"chunk_id": "chunk_unknown"})
    assert _selected_score(unknown, {}, {}) == pytest.approx(0.0)
