from rag_ingestion.chunking import ChunkLevel, ChunkingConfig, chunk_document
from rag_ingestion.models import (
    Block,
    BlockType,
    CellRangeLocator,
    Document,
    OfficePathLocator,
    SourceType,
)


def _document(*, source_type: SourceType = SourceType.DOCX) -> Document:
    return Document.from_content(
        content=b"chunking fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=source_type,
        source_uri=f"upload://fixture.{source_type.value}",
        parse_engine="native",
        parse_version="1",
        title="Policy handbook",
        domain="legal",
    )


def _paragraph(
    document: Document,
    *,
    order: int,
    text: str,
    section_path: list[str] | None = None,
) -> Block:
    return Block.from_source(
        doc_id=document.doc_id,
        order=order,
        type=BlockType.PARAGRAPH,
        text=text,
        confidence=1.0,
        locators=[
            OfficePathLocator(
                part="word/document.xml", path=f"/document/body/p[{order + 1}]"
            )
        ],
        section_path=section_path or [],
    )


def test_short_prose_builds_leaf_section_document_hierarchy() -> None:
    document = _document()
    blocks = [
        _paragraph(
            document,
            order=0,
            text="Employees must obtain approval.",
            section_path=["Travel"],
        ),
        _paragraph(
            document,
            order=1,
            text="Receipts must be retained.",
            section_path=["Travel"],
        ),
    ]

    result = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=64, leaf_max_tokens=128),
    )

    leaf = next(chunk for chunk in result.chunks if chunk.level is ChunkLevel.LEAF)
    section = next(chunk for chunk in result.chunks if chunk.level is ChunkLevel.SECTION)
    root = next(chunk for chunk in result.chunks if chunk.level is ChunkLevel.DOCUMENT)
    assert leaf.source_text == (
        "Employees must obtain approval.\n\nReceipts must be retained."
    )
    assert leaf.source_block_ids == [block.block_id for block in blocks]
    assert leaf.parent_id == section.chunk_id
    assert section.parent_id == root.chunk_id
    assert leaf.citable is True
    assert leaf.owner_user_id == "user-a"
    assert leaf.project_id == "project-a"
    assert section.citable is False
    assert root.citable is False
    assert leaf.index_text.startswith("Policy handbook / Travel\n")
    assert leaf.locators == [locator for block in blocks for locator in block.locators]


def test_special_blocks_bypass_prose_grouping_and_table_container_is_not_indexed() -> None:
    document = _document(source_type=SourceType.XLSX)
    table = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.TABLE,
        text="<table><tr><td>A</td></tr></table>",
        confidence=1.0,
        locators=[CellRangeLocator(sheet="Data", range="A1:B2")],
    )
    row = Block.from_source(
        doc_id=document.doc_id,
        order=1,
        type=BlockType.TABLE_ROW,
        text="Row 1: Product | Amount",
        confidence=1.0,
        locators=[CellRangeLocator(sheet="Data", range="A1:B1")],
        parent_block_id=table.block_id,
    )
    cell = Block.from_source(
        doc_id=document.doc_id,
        order=2,
        type=BlockType.TABLE_CELL,
        text="Product",
        confidence=1.0,
        locators=[CellRangeLocator(sheet="Data", range="A1")],
        parent_block_id=row.block_id,
    )

    result = chunk_document(document, [table, row, cell])
    leaves = [chunk for chunk in result.chunks if chunk.level is ChunkLevel.LEAF]

    assert len(leaves) == 1
    assert leaves[0].source_block_ids == [row.block_id]
    assert leaves[0].source_text == "Row 1: Product | Amount"
    assert leaves[0].metadata["block_type"] == BlockType.TABLE_ROW.value


def test_oversized_prose_splits_on_sentence_boundaries_without_overlap() -> None:
    document = _document()
    block = _paragraph(
        document,
        order=0,
        text="Alpha beta gamma. Delta epsilon zeta. Eta theta iota.",
    )
    config = ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=3, leaf_max_tokens=4)

    result = chunk_document(document, [block], config=config)
    leaves = [chunk for chunk in result.chunks if chunk.level is ChunkLevel.LEAF]

    assert [chunk.source_text for chunk in leaves] == [
        "Alpha beta gamma.",
        "Delta epsilon zeta.",
        "Eta theta iota.",
    ]
    assert [chunk.source_span for chunk in leaves] == [(0, 17), (18, 37), (38, 53)]
    assert all(chunk.source_block_ids == [block.block_id] for chunk in leaves)


def test_short_trailing_leaf_merges_with_previous_leaf_in_same_section() -> None:
    document = _document()
    blocks = [
        _paragraph(
            document,
            order=0,
            text="One two three four five six.",
            section_path=["Travel"],
        ),
        _paragraph(
            document,
            order=1,
            text="Seven eight nine ten eleven twelve thirteen.",
            section_path=["Travel"],
        ),
        _paragraph(
            document,
            order=2,
            text="Short tail.",
            section_path=["Travel"],
        ),
    ]
    config = ChunkingConfig(leaf_min_tokens=4, leaf_target_tokens=8, leaf_max_tokens=16)

    result = chunk_document(document, blocks, config=config)
    leaves = [chunk for chunk in result.chunks if chunk.level is ChunkLevel.LEAF]

    assert len(leaves) == 2
    assert leaves[1].source_block_ids == [blocks[1].block_id, blocks[2].block_id]
    assert leaves[1].source_text.endswith("Short tail.")


def test_chunk_ids_are_deterministic() -> None:
    document = _document()
    blocks = [_paragraph(document, order=0, text="Stable source text.")]

    first = chunk_document(document, blocks)
    second = chunk_document(document, blocks)

    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
