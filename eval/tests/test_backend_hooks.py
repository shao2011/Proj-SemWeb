"""The two backend hooks the harness relies on: the Auto-Merge switch (C3)
and the history window shared with the API."""

from datetime import datetime, timezone

from qdrant_client import QdrantClient

from rag_ingestion.chunking import ChunkingConfig, ChunkLevel, chunk_document
from rag_ingestion.config import RetrievalConfig
from rag_ingestion.history_window import HISTORY_REFILL_RATIO, as_turn, plan_history
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.models import Block, BlockType, Document, OfficePathLocator, SourceType
from rag_ingestion.retrieval import run_retrieval
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.storage.threads import ThreadMessage, ThreadMessageRole

_PROFILE = EmbeddingProfile(provider="cohere", model="fake-3d", version="v1", dimensions=3, task="retrieval", mode="naive")


class _Embedder:
    def embed_documents(self, items, profile, *, intent):
        return [self._vector(item) for item in items]

    def embed_query(self, text, profile, *, intent):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        return [float("speed" in lowered), float("rover" in lowered), 1.0]


class _Reranker:
    def rerank(self, query, documents, *, top_n):
        return [RankedResult(index=i, relevance_score=1.0 - i / 100) for i in range(min(top_n, len(documents)))]


def _index() -> QdrantHybridIndex:
    document = Document.from_content(
        content=b"fixture", owner_user_id="u", project_id="p", source_type=SourceType.DOCX,
        source_uri="upload://srs.docx", parse_engine="native", parse_version="1", title="SRS",
    )
    texts = [
        "The rover speed on prepared surface is 15 km/h.",
        "The rover speed on natural terrain is 10 km/h.",
        "The rover speed on slopes is 5 km/h.",
    ]
    blocks = [
        Block.from_source(
            doc_id=document.doc_id, order=i, type=BlockType.PARAGRAPH, text=text, confidence=1.0,
            locators=[OfficePathLocator(part="word/document.xml", path=f"/p[{i + 1}]")],
        )
        for i, text in enumerate(texts)
    ]
    chunks = chunk_document(
        document, blocks, config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40)
    ).chunks
    index = QdrantHybridIndex(QdrantClient(location=":memory:"), "hooks", _PROFILE)
    index.upsert(chunks, _Embedder())
    return index


def _retrieve(index, **config):
    return run_retrieval(
        index, _Embedder(), _Reranker(), "rover speed", owner_user_id="u", project_id="p",
        limit=10, config=RetrievalConfig(**config),
    )


def test_auto_merge_is_on_by_default_and_can_be_switched_off() -> None:
    index = _index()

    merged = _retrieve(index)
    plain = _retrieve(index, auto_merge=False)

    assert RetrievalConfig().auto_merge is True
    assert merged.diagnostics["auto_merged"] >= 1
    assert plain.diagnostics["auto_merged"] == 0
    assert all(chunk.level is ChunkLevel.LEAF for chunk in plain.context_chunks)
    assert [c.chunk_id for c in plain.context_chunks] == [c.chunk_id for c in plain.chunks]


def test_history_window_keeps_the_opener_and_the_newest_messages() -> None:
    messages = [
        ThreadMessage(
            message_id=f"m{p}", thread_id="t", role=ThreadMessageRole.USER if p % 2 else ThreadMessageRole.ASSISTANT,
            content="x" * 10, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), position=p,
        )
        for p in range(1, 21)
    ]

    kept, boundary = plan_history(messages, floor=6, cap=10, budget_chars=10_000)

    assert [m.position for m in kept] == [1, *range(11, 21)]
    assert boundary == 10
    assert HISTORY_REFILL_RATIO == 0.7
    assert as_turn(messages[1]).role == "assistant"
