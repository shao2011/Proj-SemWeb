from rag_ingestion.models import (
    Block,
    BlockType,
    PageBBoxLocator,
    RelationType,
)
from rag_ingestion.ingestion.reconciliation import reconcile_blocks


def _block(
    *,
    order: int,
    type: BlockType,
    text: str,
    page: int,
    bbox: tuple[float, float, float, float],
) -> Block:
    return Block.from_source(
        doc_id="doc-test",
        order=order,
        type=type,
        text=text,
        confidence=1.0,
        locators=[PageBBoxLocator(page=page, bbox=bbox)],
    )


def test_cross_page_paragraphs_remain_atomic_and_gain_continuation_relation() -> None:
    first = _block(
        order=0,
        type=BlockType.PARAGRAPH,
        text="The policy applies when",
        page=1,
        bbox=(20, 700, 500, 740),
    )
    second = _block(
        order=1,
        type=BlockType.PARAGRAPH,
        text="the employee travels abroad.",
        page=2,
        bbox=(20, 40, 500, 80),
    )

    reconciled = reconcile_blocks([first, second])

    assert [block.block_id for block in reconciled] == [first.block_id, second.block_id]
    assert reconciled[0].locators == first.locators
    assert reconciled[1].locators == second.locators
    assert reconciled[0].relations[0].type is RelationType.CONTINUES
    assert reconciled[0].relations[0].target_block_id == second.block_id


def test_cross_page_tables_are_linked_without_merging_source_blocks() -> None:
    first = _block(
        order=0,
        type=BlockType.TABLE,
        text="<table><tr><th>Item</th><th>Amount</th></tr><tr><td>A</td><td>1</td></tr></table>",
        page=1,
        bbox=(20, 500, 500, 760),
    )
    second = _block(
        order=1,
        type=BlockType.TABLE,
        text="<table><tr><td>B</td><td>2</td></tr></table>",
        page=2,
        bbox=(20, 30, 500, 200),
    )

    reconciled = reconcile_blocks([first, second])

    assert len(reconciled) == 2
    assert reconciled[0].relations[0].type is RelationType.CONTINUES
    assert reconciled[0].relations[0].target_block_id == second.block_id


def test_caption_links_to_preceding_figure() -> None:
    figure = _block(
        order=0,
        type=BlockType.FIGURE,
        text="",
        page=1,
        bbox=(20, 100, 500, 400),
    )
    caption = _block(
        order=1,
        type=BlockType.CAPTION,
        text="Figure 1. Revenue by region",
        page=1,
        bbox=(20, 405, 500, 430),
    )

    reconciled = reconcile_blocks([figure, caption])

    assert reconciled[1].relations[0].type is RelationType.CAPTION_OF
    assert reconciled[1].relations[0].target_block_id == figure.block_id


def test_repeated_top_headers_are_removed() -> None:
    headers = [
        _block(
            order=page * 2,
            type=BlockType.PARAGRAPH,
            text="Confidential policy",
            page=page,
            bbox=(20, 10, 500, 30),
        )
        for page in range(1, 4)
    ]
    bodies = [
        _block(
            order=page * 2 + 1,
            type=BlockType.PARAGRAPH,
            text=f"Body page {page}.",
            page=page,
            bbox=(20, 100, 500, 200),
        )
        for page in range(1, 4)
    ]

    reconciled = reconcile_blocks(headers + bodies)

    assert [block.text for block in reconciled] == [
        "Body page 1.",
        "Body page 2.",
        "Body page 3.",
    ]
