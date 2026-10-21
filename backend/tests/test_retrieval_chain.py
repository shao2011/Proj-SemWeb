from pathlib import Path

from qdrant_client import QdrantClient

from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    OfficePathLocator,
    Relation,
    RelationType,
    SourceType,
)
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.retrieval import RetrievalConfig, run_retrieval

_PROFILE = EmbeddingProfile(
    provider="cohere",
    model="fake-3d",
    version="v1",
    dimensions=3,
    task="retrieval",
    mode="naive",
)


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [self._vector(item) for item in items]

    def embed_query(self, text, profile, *, intent):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        return [
            float("thuế" in lowered),
            float("điều" in lowered),
            float("phạt" in lowered),
        ]


class IdentityReranker:
    def __init__(self) -> None:
        self.candidate_count = 0
        self.last_query = None

    def rerank(self, query, documents, *, top_n):
        self.candidate_count = len(documents)
        self.last_query = query
        return [
            RankedResult(index=index, relevance_score=1.0 - index / 100)
            for index in range(min(top_n, len(documents)))
        ]


def _index(tmp_path: Path, texts: list[str]) -> QdrantHybridIndex:
    del tmp_path
    document = Document.from_content(
        content=b"chain fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://law.docx",
        parse_engine="native",
        parse_version="1",
        title="Luật",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=index,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[
                OfficePathLocator(part="word/document.xml", path=f"/p[{index + 1}]")
            ],
        )
        for index, text in enumerate(texts)
    ]
    chunked = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40),
    )
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-chain-v1", _PROFILE
    )
    index.upsert(chunked.chunks, FakeEmbedder())
    return index


def _index_two_documents(
    strong: list[str], weak: list[str]
) -> tuple[QdrantHybridIndex, str, str]:
    """Two documents in one collection, one matching the query better.

    The fake embedder is binary per term, so `strong` carries both query terms
    and `weak` only one. That makes the routing order deterministic instead of a
    tie broken arbitrarily.
    """
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-chain-v1", _PROFILE
    )
    doc_ids: list[str] = []
    for position, texts in enumerate((strong, weak)):
        document = Document.from_content(
            content=f"fixture {position}".encode(),
            owner_user_id="user-a",
            project_id="project-a",
            source_type=SourceType.DOCX,
            source_uri=f"upload://doc-{position}.docx",
            parse_engine="native",
            parse_version="1",
            title=f"Tài liệu {position}",
        )
        blocks = [
            Block.from_source(
                doc_id=document.doc_id,
                order=order,
                type=BlockType.PARAGRAPH,
                text=text,
                confidence=1.0,
                locators=[
                    OfficePathLocator(
                        part="word/document.xml", path=f"/p[{order + 1}]"
                    )
                ],
            )
            for order, text in enumerate(texts)
        ]
        chunked = chunk_document(
            document,
            blocks,
            config=ChunkingConfig(
                leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40
            ),
        )
        index.upsert(chunked.chunks, FakeEmbedder())
        doc_ids.append(document.doc_id)
    return index, doc_ids[0], doc_ids[1]


def _index_with_relation(*, target: str, source: str) -> QdrantHybridIndex:
    """Two blocks where the second declares a relation to the first.

    The target text shares no query term with the source, so it can only reach
    the outcome through reference expansion rather than through search.
    """
    document = Document.from_content(
        content=b"relation fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://relations.docx",
        parse_engine="native",
        parse_version="1",
        title="Tài liệu",
    )
    target_block = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.PARAGRAPH,
        text=target,
        confidence=1.0,
        locators=[OfficePathLocator(part="word/document.xml", path="/p[1]")],
    )
    source_block = Block.from_source(
        doc_id=document.doc_id,
        order=1,
        type=BlockType.PARAGRAPH,
        text=source,
        confidence=1.0,
        locators=[OfficePathLocator(part="word/document.xml", path="/p[2]")],
        relations=[
            Relation(
                type=RelationType.CAPTION_OF, target_block_id=target_block.block_id
            )
        ],
    )
    chunked = chunk_document(
        document,
        [target_block, source_block],
        config=ChunkingConfig(
            leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40
        ),
    )
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-chain-v1", _PROFILE
    )
    index.upsert(chunked.chunks, FakeEmbedder())
    return index


class ScriptedReranker:
    """Hands back a fixed score per pool position, so a cliff can be staged."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def rerank(self, query, documents, *, top_n):
        return [
            RankedResult(index=index, relevance_score=self._scores[index])
            for index in range(min(top_n, len(documents), len(self._scores)))
        ]


def _run(
    index: QdrantHybridIndex,
    query: str,
    *,
    rewritten_query=None,
    reranker=None,
    **overrides,
):
    return run_retrieval(
        index,
        FakeEmbedder(),
        reranker or IdentityReranker(),
        query,
        owner_user_id="user-a",
        project_id="project-a",
        limit=overrides.pop("limit", 5),
        config=RetrievalConfig(**overrides),
        rewritten_query=rewritten_query,
    )


def test_candidate_budget_is_split_per_source_so_the_global_branch_survives() -> None:
    """The scoped branch must not be able to consume the whole pool.

    Slicing the merged candidates by insertion order drops whichever source ran
    last, which is the global fallback. That would silently undo the guarantee
    `global_k >= 1` exists to make, and no existing test would notice.
    """
    index, routed_doc, other_doc = _index_two_documents(
        strong=[f"Điều {number}. Quy định về thuế số {number}." for number in range(1, 5)],
        weak=[f"Mục {number}. Hướng dẫn thuế bổ sung." for number in range(1, 4)],
    )

    outcome = _run(
        index,
        "thuế điều",
        limit=10,
        route_limit=1,
        route_k=4,
        global_k=7,
        max_candidates=4,
    )

    assert outcome.routed_doc_ids == [routed_doc]
    assert other_doc in {chunk.doc_id for chunk in outcome.chunks}, (
        "the scoped branch filled the pool and starved the global fallback"
    )


def test_global_fallback_keeps_evidence_reachable_when_routing_misses(
    tmp_path: Path,
) -> None:
    index = _index(
        tmp_path,
        [
            "Điều 1. Phạm vi điều chỉnh của văn bản.",
            "Điều 2. Người nộp thuế phải kê khai đúng hạn.",
            "Điều 3. Mức phạt chậm nộp được quy định rõ.",
        ],
    )

    outcome = _run(index, "thuế", route_limit=1, route_k=1, global_k=20)

    assert outcome.chunks, "global fallback must always return candidates"
    assert all(chunk.citable for chunk in outcome.chunks)
    assert outcome.diagnostics["candidates"] >= 1


def test_retrieval_config_rejects_disabling_the_global_fallback() -> None:
    try:
        RetrievalConfig(global_k=0)
    except ValueError as error:
        assert "global fallback" in str(error)
    else:  # pragma: no cover - guard must raise
        raise AssertionError("global_k=0 must be rejected")


_FIXTURE_TEXTS = [
    "Điều một quy định về phạm vi áp dụng của luật này",
    "Điều hai giải thích các từ ngữ được dùng trong luật",
    "Điều ba nói về nguyên tắc chung khi áp dụng",
    "Điều bốn quy định về hiệu lực thi hành của luật",
]


def test_reranked_hits_far_below_the_best_one_are_dropped(tmp_path: Path) -> None:
    """The cut is relative to the best hit, never an absolute score.

    A reranker's relevance score is query-dependent -- the same 0.9 means
    different things on different queries, and the three reranker families this
    backend can be configured with do not share a distribution. So the tail is
    cut at a fraction of whatever the top hit scored, which needs no per-query
    and no per-provider calibration.
    """
    index = _index(tmp_path, _FIXTURE_TEXTS)
    outcome = _run(
        index,
        "phạm vi áp dụng",
        reranker=ScriptedReranker([1.0, 0.9, 0.2, 0.1]),
        keep_ratio=0.5,
    )
    assert outcome.diagnostics["reranked"] == 4, "the raw reranker count must stay"
    assert outcome.diagnostics["below_keep_ratio"] == 2
    assert outcome.diagnostics["evidence_leaves"] == 2
    assert all(outcome.scores[chunk.chunk_id] >= 0.5 for chunk in outcome.chunks)


def test_the_best_hit_survives_however_badly_it_scored(tmp_path: Path) -> None:
    """A weak-but-best hit is still evidence; an empty result is not.

    Nothing here can tell a bad top hit from a good one without an absolute
    floor, and there is deliberately no absolute floor. Returning nothing would
    hand the model a question with no evidence and no way to know it, so the
    argmax is kept unconditionally -- which a ratio of at most 1.0 guarantees.
    """
    index = _index(tmp_path, _FIXTURE_TEXTS)
    outcome = _run(
        index,
        "phạm vi áp dụng",
        reranker=ScriptedReranker([0.04, 0.01, 0.008, 0.002]),
        keep_ratio=0.5,
    )
    assert outcome.diagnostics["evidence_leaves"] == 1
    assert outcome.chunks, "the best hit must survive whatever it scored"
    assert outcome.scores[outcome.chunks[0].chunk_id] == 0.04


def test_signed_rerank_scores_disable_the_cut_instead_of_emptying_it(
    tmp_path: Path,
) -> None:
    """On a negative top score the ratio inverts and would cut everything.

    `relevance_score` is an unbounded float, so a reranker handing back raw
    cross-encoder logits would put the floor above the best hit. Skipping the
    cut is the only reading that keeps the never-empty guarantee.
    """
    index = _index(tmp_path, _FIXTURE_TEXTS)
    outcome = _run(
        index,
        "phạm vi áp dụng",
        reranker=ScriptedReranker([-0.4, -1.2, -3.0, -5.5]),
        keep_ratio=0.5,
    )
    assert outcome.diagnostics["below_keep_ratio"] == 0
    assert outcome.diagnostics["evidence_leaves"] == 4


def test_keep_ratio_of_zero_keeps_every_reranked_hit(tmp_path: Path) -> None:
    index = _index(tmp_path, _FIXTURE_TEXTS)
    outcome = _run(
        index,
        "phạm vi áp dụng",
        reranker=ScriptedReranker([1.0, 0.9, 0.2, 0.1]),
        keep_ratio=0.0,
    )
    assert outcome.diagnostics["below_keep_ratio"] == 0
    assert outcome.diagnostics["evidence_leaves"] == 4


def test_retrieval_config_rejects_a_keep_ratio_that_could_empty_the_result() -> None:
    """Above 1.0 the floor outruns the top hit and every candidate is cut."""
    try:
        RetrievalConfig(keep_ratio=1.1)
    except ValueError as error:
        # Not just any ValueError: `StrictModel` rejects unknown fields, so a
        # missing `keep_ratio` would pass this test for the wrong reason.
        assert "less than or equal to 1" in str(error)
    else:  # pragma: no cover - guard must raise
        raise AssertionError("keep_ratio above 1.0 must be rejected")


def test_reference_expansion_follows_block_relations(tmp_path: Path) -> None:
    """Q4 expands along whatever relations the parser produced.

    Relations now come only from reconciliation (`CAPTION_OF`, `CONTINUES`),
    which is language-neutral. The keyword-driven citation linker that used to
    feed this was deleted, so this pins the behaviour that actually survives.
    """
    del tmp_path
    index = _index_with_relation(
        target="Bảng số liệu thu ngân sách theo từng quý trong năm.",
        source="Mức phạt chậm nộp được tính theo số ngày làm việc.",
    )

    outcome = _run(index, "phạt", limit=1, route_limit=1, global_k=10)

    assert outcome.diagnostics["reference_expanded"] >= 1
    texts = " ".join(chunk.source_text for chunk in outcome.chunks)
    assert "Bảng số liệu" in texts, (
        "the related block must arrive through expansion, not through search"
    )


def test_without_a_relation_the_same_block_is_unreachable(tmp_path: Path) -> None:
    """Negative control for the test above.

    The target shares no term with the query, so if it ever shows up without a
    relation, the test above is passing because search found it and not because
    expansion did any work.
    """
    index = _index(
        tmp_path,
        [
            "Bảng số liệu thu ngân sách theo từng quý trong năm.",
            "Mức phạt chậm nộp được tính theo số ngày làm việc.",
        ],
    )

    outcome = _run(index, "phạt", limit=1, route_limit=1, global_k=10)

    assert outcome.diagnostics["reference_expanded"] == 0
    texts = " ".join(chunk.source_text for chunk in outcome.chunks)
    assert "Bảng số liệu" not in texts


def test_empty_index_returns_an_empty_outcome(tmp_path: Path) -> None:
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-empty-v1", _PROFILE
    )
    index.ensure_collection()

    outcome = _run(index, "thuế")

    assert outcome.chunks == []
    assert outcome.evidence_leaf_ids == []


def test_rewritten_query_drives_the_rerank_text(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [
            "Điều 1. Phạm vi điều chỉnh của văn bản.",
            "Điều 2. Người nộp thuế phải kê khai đúng hạn.",
        ],
    )
    reranker = IdentityReranker()

    run_retrieval(
        index,
        FakeEmbedder(),
        reranker,
        "thuế",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        config=RetrievalConfig(route_limit=1, global_k=10),
        rewritten_query="điều",
    )

    assert reranker.last_query == "điều"


def test_no_rewritten_query_reranks_on_the_original(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        ["Điều 1. Phạm vi điều chỉnh của văn bản."],
    )
    reranker = IdentityReranker()

    run_retrieval(
        index,
        FakeEmbedder(),
        reranker,
        "thuế",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        config=RetrievalConfig(route_limit=1, global_k=10),
    )

    assert reranker.last_query == "thuế"


def test_rewrite_diagnostics_reflect_whether_a_rewrite_was_used(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path, ["Điều 1. Phạm vi điều chỉnh của văn bản."])

    without = _run(index, "thuế", route_limit=1, global_k=10)
    assert without.diagnostics["rewrite_used"] == 0

    with_rewrite = _run(
        index, "thuế", rewritten_query="điều", route_limit=1, global_k=10
    )
    assert with_rewrite.diagnostics["rewrite_used"] == 1


def test_rewritten_query_adds_a_source_without_starving_the_global_fallback() -> None:
    """Same regression as the two-source case, now with three sources active.

    `_fill_pool` already round-robins over however many sources it is given,
    but the call site has to actually add the third source rather than only
    ever passing two -- this is the test that would catch a rewrite source
    silently swallowing the fallback's share of a tight budget.
    """
    index, routed_doc, other_doc = _index_two_documents(
        strong=[f"Điều {number}. Quy định về thuế số {number}." for number in range(1, 5)],
        weak=[f"Mục {number}. Hướng dẫn thuế bổ sung." for number in range(1, 4)],
    )

    outcome = _run(
        index,
        "thuế điều",
        rewritten_query="thuế điều",
        limit=10,
        route_limit=1,
        route_k=4,
        global_k=7,
        max_candidates=4,
    )

    assert outcome.routed_doc_ids == [routed_doc]
    assert other_doc in {chunk.doc_id for chunk in outcome.chunks}, (
        "adding the rewrite source starved the global fallback"
    )


def test_rewritten_query_with_no_routed_docs_only_affects_rerank(
    tmp_path: Path,
) -> None:
    """Routing on an empty collection yields no `routed_doc_ids`, so there is
    no scoped branch to rewrite. The rewrite must still reach the reranker."""
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-empty-v1", _PROFILE
    )
    index.ensure_collection()
    reranker = IdentityReranker()

    outcome = run_retrieval(
        index,
        FakeEmbedder(),
        reranker,
        "thuế",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        config=RetrievalConfig(),
        rewritten_query="điều",
    )

    assert outcome.routed_doc_ids == []
    assert outcome.chunks == []


def test_auto_merged_parent_lands_in_context_chunks_with_full_text(tmp_path: Path) -> None:
    from rag_ingestion.chunking import ChunkLevel
    from rag_ingestion.retrieval.automerge import descendant_leaf_ids

    sentences = [
        "Điều 1 quy định thuế suất cơ bản cho hộ kinh doanh.",
        "Điều 2 quy định thuế bổ sung cho doanh nghiệp lớn.",
        "Điều 3 quy định thuế đặc biệt cho hàng nhập khẩu.",
    ]
    index = _index(tmp_path, sentences)

    outcome = run_retrieval(
        index,
        FakeEmbedder(),
        IdentityReranker(),
        "thuế điều",
        owner_user_id="user-a",
        project_id="project-a",
        limit=10,
    )

    assert outcome.diagnostics["auto_merged"] >= 1
    merged = outcome.context_chunks[0]
    assert merged.level is not ChunkLevel.LEAF
    for sentence in sentences:
        assert sentence in merged.source_text, (
            "the merged parent must carry the section's contiguous text"
        )
    # Rank order: the first context chunk is the one covering the best leaf.
    chunks_by_id = {chunk.chunk_id: chunk for chunk in outcome.chunks}
    covered = set(descendant_leaf_ids(merged, chunks_by_id)) | {merged.chunk_id}
    assert outcome.chunks[0].chunk_id in covered


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.query_calls = 0

    def embed_query(self, text, profile, *, intent):
        self.query_calls += 1
        return super().embed_query(text, profile, intent=intent)


def test_query_is_embedded_exactly_once_per_retrieval(tmp_path: Path) -> None:
    index = _index(tmp_path, ["Điều 1 quy định thuế suất."])
    embedder = CountingEmbedder()

    run_retrieval(
        index,
        embedder,
        IdentityReranker(),
        "thuế điều",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
    )

    assert embedder.query_calls == 1


def test_a_rewrite_costs_exactly_one_extra_embedding(tmp_path: Path) -> None:
    index = _index(tmp_path, ["Điều 1 quy định thuế suất.", "Điều 2 quy định phạt."])
    embedder = CountingEmbedder()

    run_retrieval(
        index,
        embedder,
        IdentityReranker(),
        "thuế điều",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        rewritten_query="phạt điều",
    )

    assert embedder.query_calls == 2


def test_scoped_search_is_skipped_when_routing_covers_the_whole_project(
    tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    index = _index(tmp_path, ["Điều 1 quy định thuế suất cơ bản cho hộ kinh doanh."])
    first = run_retrieval(
        index,
        FakeEmbedder(),
        IdentityReranker(),
        "thuế điều",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
    )
    assert first.routed_doc_ids, "sanity: routing found the document"

    index.client.query_points = Mock(wraps=index.client.query_points)
    covered = run_retrieval(
        index,
        FakeEmbedder(),
        IdentityReranker(),
        "thuế điều",
        owner_user_id="user-a",
        project_id="project-a",
        limit=5,
        project_doc_ids=set(first.routed_doc_ids),
    )

    # Q0 + global only: the doc-scoped pass would repeat the global search
    # with a filter that excludes nothing.
    assert index.client.query_points.call_count == 2
    assert covered.chunks, "skipping the redundant pass must not lose evidence"
