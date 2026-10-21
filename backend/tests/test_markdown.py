import json

from rag_ingestion.chunking import ChunkLevel, ChunkingConfig, chunk_document
from rag_ingestion.ingestion.markdown import markdown_keys, render, store
from rag_ingestion.models import (
    Block,
    BlockType,
    CellRangeLocator,
    Document,
    OfficePathLocator,
    PageBBoxLocator,
    SourceType,
)
from rag_ingestion.storage.media import LocalMediaStore


def _document(title: str = "Policy handbook") -> Document:
    return Document.from_content(
        content=b"markdown fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title=title,
    )


def _block(document: Document, order: int, type: BlockType, text: str, **extra) -> Block:
    return Block.from_source(
        doc_id=document.doc_id,
        order=order,
        type=type,
        text=text,
        confidence=1.0,
        locators=[OfficePathLocator(part="word/document.xml", path=f"/p[{order + 1}]")],
        **extra,
    )


def test_headings_and_prose_render_in_document_order() -> None:
    document = _document()
    blocks = [
        _block(document, 0, BlockType.HEADING, "Travel", section_path=["Travel"]),
        _block(document, 1, BlockType.PARAGRAPH, "Requests require code ZX-42.", section_path=["Travel"]),
        _block(document, 2, BlockType.HEADING, "Security", section_path=["Security"]),
        _block(document, 3, BlockType.PARAGRAPH, "Incidents use code SEC-9.", section_path=["Security"]),
    ]

    markdown, anchors = render(document, blocks)

    assert markdown.index("Travel") < markdown.index("ZX-42")
    assert markdown.index("ZX-42") < markdown.index("Security")
    assert "# Travel" in markdown
    assert "Requests require code ZX-42." in markdown


def test_every_anchor_points_at_the_text_it_names() -> None:
    """The offsets are the whole contract: the panel highlights `markdown[start:end]`."""
    document = _document()
    blocks = [
        _block(document, 0, BlockType.PARAGRAPH, "Đoạn thứ nhất có dấu tiếng Việt."),
        _block(document, 1, BlockType.PARAGRAPH, "Second paragraph here."),
    ]

    markdown, anchors = render(document, blocks)

    for block in blocks:
        start, end = anchors[block.block_id]
        assert markdown[start:end] == block.text


def test_headings_are_rendered_but_are_not_anchor_targets() -> None:
    """The chunker never puts a heading into a chunk -- it only flushes prose and
    sets `section_path` -- so no citation can ever reference one."""
    document = _document()
    heading = _block(document, 0, BlockType.HEADING, "Travel")
    paragraph = _block(document, 1, BlockType.PARAGRAPH, "Body text.")

    markdown, anchors = render(document, [heading, paragraph])

    assert "Travel" in markdown
    assert heading.block_id not in anchors
    assert paragraph.block_id in anchors


def test_a_table_container_with_rows_is_not_rendered_twice() -> None:
    document = _document()
    table = _block(document, 0, BlockType.TABLE, "Code | Meaning")
    row = Block.from_source(
        doc_id=document.doc_id,
        order=1,
        type=BlockType.TABLE_ROW,
        text="ZX-42 | Travel",
        confidence=1.0,
        parent_block_id=table.block_id,
        locators=[OfficePathLocator(part="word/document.xml", path="/tbl/tr[1]")],
    )

    markdown, anchors = render(document, [table, row])

    assert markdown.count("ZX-42 | Travel") == 1
    assert table.block_id not in anchors, "the container is skipped when rows exist"
    assert row.block_id in anchors


def test_a_cell_is_dropped_when_its_row_already_carries_the_text() -> None:
    document = _document()
    row = _block(document, 0, BlockType.TABLE_ROW, "ZX-42 | Travel")
    cell = Block.from_source(
        doc_id=document.doc_id,
        order=1,
        type=BlockType.TABLE_CELL,
        text="ZX-42",
        confidence=1.0,
        parent_block_id=row.block_id,
        locators=[OfficePathLocator(part="word/document.xml", path="/tbl/tr[1]/tc[1]")],
    )

    markdown, anchors = render(document, [row, cell])

    assert cell.block_id not in anchors
    assert row.block_id in anchors


def test_special_block_types_get_their_markdown_form() -> None:
    document = _document()
    blocks = [
        _block(document, 0, BlockType.LIST, "First item"),
        _block(document, 1, BlockType.CODE, "print('hi')"),
        _block(document, 2, BlockType.FORMULA, "E = mc^2"),
        _block(document, 3, BlockType.CAPTION, "Figure 1: the flow"),
    ]

    markdown, anchors = render(document, blocks)

    assert "- First item" in markdown
    assert "```" in markdown and "print('hi')" in markdown
    assert "$$" in markdown and "E = mc^2" in markdown
    assert "*Figure 1: the flow*" in markdown
    assert all(block.block_id in anchors for block in blocks)


def test_a_figure_with_an_image_renders_a_link_to_it() -> None:
    document = _document()
    figure = _block(
        document, 0, BlockType.FIGURE, "Diagram of the process",
        media_ref="media/doc_1/page/3.png",
    )

    markdown, anchors = render(document, [figure])

    assert "![](media/doc_1/page/3.png)" in markdown
    assert "Diagram of the process" in markdown
    start, end = anchors[figure.block_id]
    assert markdown[start:end] == figure.text
    assert markdown.index("![](") < start, "the picture comes before its caption"


def test_spreadsheet_rows_render_without_an_office_path() -> None:
    document = _document("Budget")
    row = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.TABLE_ROW,
        text="Row 2: Hanoi | 1200",
        confidence=1.0,
        locators=[CellRangeLocator(sheet="Sheet1", range="A2:B2")],
    )

    markdown, anchors = render(document, [row])

    assert "Row 2: Hanoi | 1200" in markdown
    assert row.block_id in anchors


def _grid(document: Document, sheet: str = "Data") -> list[Block]:
    header = Block.from_source(
        doc_id=document.doc_id,
        order=0,
        type=BlockType.TABLE,
        text="| # | A | B |\n|---|---|---|",
        confidence=1.0,
        section_path=[sheet],
        locators=[CellRangeLocator(sheet=sheet, range="A1:B2")],
        metadata={"grid": sheet},
    )
    rows = [
        Block.from_source(
            doc_id=document.doc_id,
            order=order,
            type=BlockType.TABLE_ROW,
            text=text,
            confidence=1.0,
            parent_block_id=header.block_id,
            section_path=[sheet],
            locators=[CellRangeLocator(sheet=sheet, range=f"A{order}:B{order}")],
            metadata={"grid": sheet, "row": order},
        )
        for order, text in ((1, "| 1 | Hùng | 38 |"), (2, "| 2 | Ngọc | 3.75 |"))
    ]
    return [header, *rows]


def test_grid_rows_render_as_one_unbroken_markdown_table() -> None:
    """A blank line between rows ends the table, so GFM would print the pipes raw."""
    document = _document("Budget")
    blocks = _grid(document)

    markdown, _ = render(document, blocks)

    table = markdown[markdown.index("| # |") :].strip()
    assert table.splitlines() == [
        "| # | A | B |",
        "|---|---|---|",
        "| 1 | Hùng | 38 |",
        "| 2 | Ngọc | 3.75 |",
    ]


def test_a_grid_header_survives_even_though_it_owns_the_rows() -> None:
    """The generic rule drops a table container once its rows are present; a grid
    header is the markdown syntax those rows need, so it is the exception."""
    document = _document("Budget")
    blocks = _grid(document)

    markdown, anchors = render(document, blocks)

    assert "|---|---|---|" in markdown
    assert all(block.block_id in anchors for block in blocks[1:])


def test_every_grid_row_anchor_still_points_at_its_own_line() -> None:
    document = _document("Budget")
    blocks = _grid(document)

    markdown, anchors = render(document, blocks)

    for row in blocks[1:]:
        start, end = anchors[row.block_id]
        assert markdown[start:end] == row.text


def test_two_grids_do_not_run_into_each_other() -> None:
    document = _document("Budget")
    first = _grid(document, "Công nợ")
    second = [
        block.model_copy(update={"order": block.order + 10})
        for block in _grid(document, "QR")
    ]

    markdown, _ = render(document, [*first, *second])

    assert "| 2 | Ngọc | 3.75 |\n\n| # | A | B |" in markdown


def test_a_pdf_page_renders_headings_from_its_section_path_depth() -> None:
    document = _document("Regulation 42")
    blocks = [
        Block.from_source(
            doc_id=document.doc_id, order=0, type=BlockType.HEADING,
            text="Chương I", confidence=1.0, section_path=["Chương I"],
            locators=[PageBBoxLocator(page=1, bbox=(0.1, 0.1, 0.9, 0.2))],
        ),
        Block.from_source(
            doc_id=document.doc_id, order=1, type=BlockType.HEADING,
            text="Điều 3", confidence=1.0, section_path=["Chương I", "Điều 3"],
            locators=[PageBBoxLocator(page=1, bbox=(0.1, 0.3, 0.9, 0.4))],
        ),
        Block.from_source(
            doc_id=document.doc_id, order=2, type=BlockType.PARAGRAPH,
            text="Nội dung điều ba.", confidence=1.0,
            section_path=["Chương I", "Điều 3"],
            locators=[PageBBoxLocator(page=1, bbox=(0.1, 0.5, 0.9, 0.6))],
        ),
    ]

    markdown, anchors = render(document, blocks)

    assert "# Chương I" in markdown
    assert "## Điều 3" in markdown
    start, end = anchors[blocks[2].block_id]
    assert markdown[start:end] == "Nội dung điều ba."


def test_slides_render_as_their_own_section() -> None:
    document = _document("Deck")
    slide = _block(document, 0, BlockType.SLIDE, "Slide 2: Roadmap")
    body = _block(document, 1, BlockType.PARAGRAPH, "Ship in Q3.")

    markdown, anchors = render(document, [slide, body])

    assert "## Slide 2: Roadmap" in markdown
    start, end = anchors[slide.block_id]
    assert markdown[start:end] == slide.text
    assert body.block_id in anchors


def test_empty_document_renders_without_failing() -> None:
    markdown, anchors = render(_document(), [])

    assert isinstance(markdown, str)
    assert anchors == {}


def test_blank_blocks_are_skipped() -> None:
    document = _document()
    blank = _block(document, 0, BlockType.PARAGRAPH, "   ")
    real = _block(document, 1, BlockType.PARAGRAPH, "Real text.")

    markdown, anchors = render(document, [blank, real])

    assert blank.block_id not in anchors
    assert real.block_id in anchors


def test_store_round_trips_both_artifacts_under_derivable_keys(tmp_path) -> None:
    """The route reconstructs these keys from `doc_id` alone, so they must not
    depend on anything that is gone by read time."""
    document = _document()
    blocks = [_block(document, 0, BlockType.PARAGRAPH, "Requests require code ZX-42.")]
    media = LocalMediaStore(tmp_path)

    markdown_ref, anchors_ref = store(document, blocks, media)

    assert (markdown_ref, anchors_ref) == markdown_keys(document.doc_id)
    markdown = media.get(markdown_ref).decode()
    anchors = json.loads(media.get(anchors_ref))
    start, end = anchors[blocks[0].block_id]
    assert markdown[start:end] == blocks[0].text


def test_stored_offsets_survive_the_json_round_trip_for_non_ascii(tmp_path) -> None:
    """Offsets are character positions, so the file must be read back as text
    decoded the same way it was written -- a byte offset would be wrong here."""
    document = _document("Quy định")
    block = _block(document, 0, BlockType.PARAGRAPH, "Điều 3 quy định về nghỉ phép.")
    media = LocalMediaStore(tmp_path)

    markdown_ref, anchors_ref = store(document, [block], media)

    markdown = media.get(markdown_ref).decode()
    start, end = json.loads(media.get(anchors_ref))[block.block_id]
    assert markdown[start:end] == block.text


def test_every_block_a_chunk_cites_has_an_anchor() -> None:
    """The invariant the whole feature rests on.

    A citation carries `source_block_ids`. If the renderer drops a block that
    the chunker kept, clicking that citation lands nowhere and the failure is
    silent -- the panel simply does not scroll.
    """
    document = _document()
    blocks = [
        _block(document, 0, BlockType.HEADING, "Travel", section_path=["Travel"]),
        _block(document, 1, BlockType.PARAGRAPH, "Requests require code ZX-42.", section_path=["Travel"]),
        _block(document, 2, BlockType.LIST, "Submit before travel.", section_path=["Travel"]),
        _block(document, 3, BlockType.TABLE_ROW, "ZX-42 | Travel", section_path=["Travel"]),
        _block(document, 4, BlockType.PARAGRAPH, "Incidents use code SEC-9.", section_path=["Travel"]),
    ]
    chunked = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=8, leaf_max_tokens=40),
    )
    _, anchors = render(document, blocks)

    cited = {
        block_id
        for chunk in chunked.chunks
        if chunk.level is ChunkLevel.LEAF
        for block_id in chunk.source_block_ids
    }
    assert cited, "fixture must produce at least one citable chunk"
    assert cited <= set(anchors), (
        f"blocks reachable by citation but missing an anchor: {cited - set(anchors)}"
    )
