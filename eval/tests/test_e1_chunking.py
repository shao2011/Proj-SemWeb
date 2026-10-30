import re

from rag_ingestion.chunking import ChunkLevel
from rag_ingestion.models import Block, BlockType, Document, ParseResult, PageBBoxLocator, SourceType
from rag_ingestion.retrieval import RetrievalOutcome

from eval import settings
from eval.data import RequestaItem
from eval.e1_chunking import (
    chunk_fixed,
    chunk_semantic,
    chunk_structure,
    context_evidence,
    late_vectors,
    retrieval_row,
    section_groups,
    semantic_breaks,
    without_prefix,
)
from eval.jina import PrecomputedEmbedder, pack_groups


def _parsed(paragraphs: list[str]) -> ParseResult:
    document = Document.from_content(
        content=b"srs",
        owner_user_id="eval",
        project_id="p",
        source_type=SourceType.PDF,
        source_uri="upload://SRS_Test.pdf",
        parse_engine="native",
        parse_version="1",
        title="SRS_Test",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=index,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[PageBBoxLocator(page=index + 1, bbox=(0, 0, 10, 10))],
        )
        for index, text in enumerate(paragraphs)
    ]
    return ParseResult(document=document, blocks=blocks)


def test_fixed_windows_have_256_words_and_no_prefix() -> None:
    parsed = _parsed([" ".join(f"w{i}" for i in range(300)), " ".join(f"v{i}" for i in range(300))])

    chunks = chunk_fixed(parsed)

    sizes = [len(c.source_text.split()) for c in chunks]
    assert sizes == [256, 256, 88]
    assert all(c.index_text == c.source_text for c in chunks)
    assert all(c.level is ChunkLevel.LEAF and c.citable for c in chunks)
    # The second window crosses from the first paragraph into the second.
    assert len(chunks[1].source_block_ids) == 2


def test_semantic_breaks_cut_only_above_the_percentile() -> None:
    same, other = [1.0, 0.0], [0.0, 1.0]
    vectors = [same] * 10 + [other] * 10

    assert semantic_breaks(vectors, 95) == [9]
    assert semantic_breaks([same], 95) == []


def test_semantic_chunks_follow_the_breaks() -> None:
    parsed = _parsed(["Alpha one. Alpha two. Alpha three.", "Beta one. Beta two."])

    def embed(texts):
        return [[1.0, 0.0] if t.startswith("Alpha") else [0.0, 1.0] for t in texts]

    chunks = chunk_semantic(parsed, embed, percentile=50)

    assert [c.source_text for c in chunks] == [
        "Alpha one. Alpha two. Alpha three.",
        "Beta one. Beta two.",
    ]


def test_structure_chunks_carry_the_heading_prefix_and_late_chunks_do_not() -> None:
    parsed = _parsed(["3.2.2 Mission Phases", "The phase shall end within 5 seconds. " * 30])

    leaves = [c for c in chunk_structure(parsed) if c.level is ChunkLevel.LEAF]
    plain = without_prefix(leaves)

    assert all(c.index_text.startswith("SRS_Test") for c in leaves)
    assert all(c.index_text == c.source_text for c in plain)
    assert [c.chunk_id for c in plain] == [c.chunk_id for c in leaves]


def test_late_chunking_sends_whole_sections_and_maps_vectors_back() -> None:
    parsed = _parsed(["Intro text here. " * 40, "More text here. " * 40])
    chunks = without_prefix(chunk_structure(parsed))
    sent: list[list[str]] = []

    class FakeLate:
        def embed(self, texts):
            sent.append(texts)
            return [[float(i), 1.0] for i in range(len(texts))]

    vectors = late_vectors(chunks, FakeLate())

    leaves = [c.source_text for c in chunks if c.level is ChunkLevel.LEAF]
    assert set(vectors) == set(leaves)
    assert sum(len(request) for request in sent) == len(leaves)
    assert section_groups(chunks)


def test_pack_groups_keeps_sections_whole_unless_they_overflow() -> None:
    small, big = ["a" * 40] * 2, ["b" * 400] * 3  # 11 tokens each / 101 tokens each

    assert pack_groups([small, small], max_tokens=50) == [small + small]
    assert pack_groups([small, small], max_tokens=30) == [small, small]
    assert pack_groups([big], max_tokens=150) == [big[:1], big[1:2], big[2:]]


def test_precomputed_embedder_falls_back_for_unknown_texts() -> None:
    class Delegate:
        def embed_documents(self, items, profile, *, intent):
            return [[9.0] for _ in items]

        def embed_query(self, text, profile, *, intent):
            return [7.0]

    embedder = PrecomputedEmbedder({"known": [1.0]}, Delegate())

    assert embedder.embed_documents(["known", "new"], None, intent="document") == [[1.0], [9.0]]
    assert embedder.embed_query("q", None, intent="query") == [7.0]


def test_retrieval_row_scores_the_ranked_leaves() -> None:
    parsed = _parsed(["The AMSC Number will be assigned by the Director. " * 3, "Unrelated text. " * 20])
    leaves = [c for c in chunk_fixed(parsed, window=10)]
    item = RequestaItem(1, "Test", 1, "AMSC Number will be assigned by the Director", "who?", "the Director")
    outcome = RetrievalOutcome(chunks=[leaves[-1], leaves[0]], context_chunks=[leaves[-1], leaves[0]])

    row = retrieval_row(item, outcome)

    assert row["hit"] == 1.0
    assert row["mrr"] == 0.5
    assert row["context_tokens"] == sum(len(c.source_text.split()) for c in outcome.context_chunks)
    assert [e.chunk_id for e in context_evidence(outcome)] == [leaves[-1].chunk_id, leaves[0].chunk_id]


def test_only_c5_runs_auto_merge() -> None:
    assert settings.AUTO_MERGE_CONFIGS == ("C5",)
    assert re.search(r"auto_merge=config_id in settings.AUTO_MERGE_CONFIGS", open("eval/e1_chunking.py").read())
