from rag_ingestion.retrieval.automerge import auto_merge, descendant_leaf_ids
from rag_ingestion.chunking import Chunk, ChunkLevel


def _leaf(chunk_id: str, text: str, parent_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc-1",
        owner_user_id="user-a",
        project_id="project-a",
        level=ChunkLevel.LEAF,
        source_text=text,
        index_text=f"Doc\n{text}",
        source_block_ids=[f"blk-{chunk_id}"],
        locators=[],
        confidence=1.0,
        citable=True,
        parent_id=parent_id,
    )


def _parent(
    chunk_id: str, children: list[Chunk], parent_id: str | None = None
) -> Chunk:
    text = "\n\n".join(child.source_text for child in children)
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc-1",
        owner_user_id="user-a",
        project_id="project-a",
        level=ChunkLevel.SECTION if parent_id else ChunkLevel.DOCUMENT,
        source_text=text,
        index_text=f"Doc\n{text}",
        source_block_ids=[
            block_id for child in children for block_id in child.source_block_ids
        ],
        locators=[],
        confidence=1.0,
        citable=False,
        parent_id=parent_id,
        child_ids=[child.chunk_id for child in children],
    )


def _fixture() -> tuple[list[Chunk], dict[str, Chunk]]:
    leaves = [_leaf(f"leaf-{index}", f"sentence {index} " * 10, "sec-1") for index in range(3)]
    section = _parent("sec-1", leaves, parent_id="doc-root")
    root = _parent("doc-root", [section])
    registry = {chunk.chunk_id: chunk for chunk in [*leaves, section, root]}
    return leaves, registry


def test_single_recalled_child_is_not_promoted() -> None:
    leaves, registry = _fixture()

    merged = auto_merge([leaves[0]], registry, token_budget=4_000)

    assert merged.merge_count == 0
    assert [chunk.chunk_id for chunk in merged.chunks] == ["leaf-0"]
    assert merged.evidence_leaf_ids == ["leaf-0"]


def test_two_recalled_siblings_promote_to_the_parent() -> None:
    leaves, registry = _fixture()

    merged = auto_merge(leaves[:2], registry, token_budget=4_000)

    assert merged.merge_count >= 1
    assert any(chunk.chunk_id == "sec-1" for chunk in merged.chunks)
    assert set(merged.evidence_leaf_ids) == {"leaf-0", "leaf-1", "leaf-2"}


def test_promotion_is_blocked_when_the_parent_does_not_fit_the_budget() -> None:
    leaves, registry = _fixture()

    merged = auto_merge(leaves[:2], registry, token_budget=25)

    assert merged.merge_count == 0
    assert {chunk.chunk_id for chunk in merged.chunks} == {"leaf-0", "leaf-1"}


def test_only_child_resolves_directly_to_its_parent() -> None:
    leaf = _leaf("leaf-solo", "only child text", "sec-solo")
    section = _parent("sec-solo", [leaf], parent_id="doc-solo")
    root = _parent("doc-solo", [section])
    registry = {chunk.chunk_id: chunk for chunk in [leaf, section, root]}

    merged = auto_merge([leaf], registry, token_budget=4_000)

    assert [chunk.chunk_id for chunk in merged.chunks] == ["sec-solo"]
    assert merged.evidence_leaf_ids == ["leaf-solo"]


def test_zero_budget_returns_nothing() -> None:
    leaves, registry = _fixture()

    assert auto_merge(leaves, registry, token_budget=0).chunks == []


def test_descendant_leaf_ids_walks_multiple_levels() -> None:
    _, registry = _fixture()

    assert descendant_leaf_ids(registry["doc-root"], registry) == [
        "leaf-0",
        "leaf-1",
        "leaf-2",
    ]
