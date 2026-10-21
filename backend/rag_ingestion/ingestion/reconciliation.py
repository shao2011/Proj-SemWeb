from __future__ import annotations

import re
from collections import defaultdict

from rag_ingestion.models import (
    Block,
    BlockType,
    PageBBoxLocator,
    Relation,
    RelationType,
)


def _page_locator(block: Block) -> PageBBoxLocator | None:
    return next(
        (locator for locator in block.locators if isinstance(locator, PageBBoxLocator)),
        None,
    )


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _repeated_top_header_ids(blocks: list[Block]) -> set[str]:
    occurrences: dict[str, list[Block]] = defaultdict(list)
    for block in blocks:
        locator = _page_locator(block)
        if (
            locator is not None
            and locator.bbox[1] <= 72
            and 0 < len(block.text) <= 200
            and block.type in {BlockType.PARAGRAPH, BlockType.HEADING}
        ):
            occurrences[_normalized_text(block.text)].append(block)

    repeated_ids: set[str] = set()
    for candidates in occurrences.values():
        pages = {
            locator.page
            for block in candidates
            if (locator := _page_locator(block)) is not None
        }
        if len(pages) >= 3:
            repeated_ids.update(block.block_id for block in candidates)
    return repeated_ids


def _add_relation(block: Block, relation_type: RelationType, target_id: str) -> None:
    if any(
        relation.type is relation_type and relation.target_block_id == target_id
        for relation in block.relations
    ):
        return
    block.relations.append(Relation(type=relation_type, target_block_id=target_id))


def _horizontal_overlap(first: PageBBoxLocator, second: PageBBoxLocator) -> float:
    overlap = max(0.0, min(first.bbox[2], second.bbox[2]) - max(first.bbox[0], second.bbox[0]))
    narrowest_width = max(min(first.bbox[2] - first.bbox[0], second.bbox[2] - second.bbox[0]), 1.0)
    return overlap / narrowest_width


def _is_cross_page_continuation(first: Block, second: Block) -> bool:
    first_locator = _page_locator(first)
    second_locator = _page_locator(second)
    if (
        first_locator is None
        or second_locator is None
        or second_locator.page != first_locator.page + 1
        or second_locator.bbox[1] > 100
    ):
        return False

    if first.type is BlockType.TABLE and second.type is BlockType.TABLE:
        return _horizontal_overlap(first_locator, second_locator) >= 0.7
    if first.type is not BlockType.PARAGRAPH or second.type is not BlockType.PARAGRAPH:
        return False
    if re.search(r"[.!?:;][\"')\]]?\s*$", first.text):
        return False
    next_text = second.text.lstrip()
    return bool(next_text) and (next_text[0].islower() or next_text[0] in ",;:)")


def reconcile_blocks(blocks: list[Block]) -> list[Block]:
    repeated_header_ids = _repeated_top_header_ids(blocks)
    reconciled = [
        block.model_copy(deep=True)
        for block in blocks
        if block.block_id not in repeated_header_ids
    ]

    for index, block in enumerate(reconciled):
        if block.type is BlockType.CAPTION:
            caption_locator = _page_locator(block)
            for candidate in reversed(reconciled[:index]):
                candidate_locator = _page_locator(candidate)
                if (
                    candidate.type is BlockType.FIGURE
                    and caption_locator is not None
                    and candidate_locator is not None
                    and candidate_locator.page == caption_locator.page
                ):
                    _add_relation(block, RelationType.CAPTION_OF, candidate.block_id)
                    break

    for first, second in zip(reconciled, reconciled[1:], strict=False):
        if _is_cross_page_continuation(first, second):
            _add_relation(first, RelationType.CONTINUES, second.block_id)

    return reconciled
