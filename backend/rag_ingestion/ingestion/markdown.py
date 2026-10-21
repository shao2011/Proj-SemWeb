"""Render a parsed document to markdown, with an offset index per block.

This exists because a citation needs somewhere to point. Chunks reach Qdrant
but `Block[]` is never stored and the uploaded file is deleted after ingest, so
a document cannot be reassembled later: the markdown has to be produced here,
while the blocks are still in hand.

Two artifacts come out. The markdown is what a reader sees. The anchor index
maps `block_id` to a `[start, end)` character range inside that markdown, which
is what lets a citation *highlight a passage* rather than merely scroll near
it -- a chunk usually covers several blocks, so a single scroll target would
under-sell what was actually cited.

The skip rules below mirror `chunking.chunker` exactly. That is not tidiness:
a citation carries `source_block_ids`, so any block the chunker keeps and this
renderer drops becomes a citation that lands nowhere, and it fails silently.
"""

from __future__ import annotations

import json

from rag_ingestion.models import Block, BlockType, Document
from rag_ingestion.storage.media import MediaStore, media_key

def _heading_level(block: Block) -> int:
    """Depth from `section_path`, clamped to what markdown supports."""
    return min(max(len(block.section_path), 1), 6)


def _grid(block: Block) -> str | None:
    """Which spreadsheet grid a block belongs to, if any.

    A grid is a real markdown table: its header, divider and rows have to come
    out as consecutive lines, because one blank line between them ends the
    table and GFM prints the remaining pipes as literal text.
    """
    grid = block.metadata.get("grid")
    return grid if isinstance(grid, str) else None


def _rendered(block: Block) -> tuple[str, str]:
    """Return the prefix and suffix that wrap a block's own text.

    The text itself is never altered, so the anchor can point straight at it
    and `markdown[start:end]` is exactly what the block said.
    """
    if block.type is BlockType.LIST:
        return "- ", ""
    if block.type is BlockType.CODE:
        return "```\n", "\n```"
    if block.type is BlockType.FORMULA:
        return "$$\n", "\n$$"
    if block.type is BlockType.CAPTION:
        return "*", "*"
    if block.type is BlockType.SPEECH_TURN:
        return "> ", ""
    if block.type is BlockType.SLIDE:
        return "## ", ""
    return "", ""


def render(
    document: Document, blocks: list[Block]
) -> tuple[str, dict[str, tuple[int, int]]]:
    """Render blocks to markdown and index where each one landed."""
    ordered = sorted(blocks, key=lambda item: item.order)

    # A container is redundant once its parts are present separately, and
    # rendering both would duplicate the text on the page.
    rows_by_parent = {
        block.parent_block_id
        for block in ordered
        if block.type is BlockType.TABLE_ROW and block.parent_block_id
    }
    cells_by_parent = {
        block.parent_block_id
        for block in ordered
        if block.type is BlockType.TABLE_CELL and block.parent_block_id
    }

    renderable = [
        block
        for block in ordered
        if block.text.strip()
        and not (
            block.type is BlockType.TABLE
            and block.block_id in rows_by_parent
            # A grid header carries the markdown syntax its rows need, so it is
            # the one container that stays even though the rows are present.
            and _grid(block) is None
        )
        and not (
            block.type is BlockType.TABLE_CELL
            and block.parent_block_id in cells_by_parent
        )
    ]

    parts: list[str] = []
    anchors: dict[str, tuple[int, int]] = {}
    length = 0

    def append(text: str) -> None:
        nonlocal length
        parts.append(text)
        length += len(text)

    if document.title:
        append(f"# {document.title}\n\n")

    for position, block in enumerate(renderable):
        following = renderable[position + 1] if position + 1 < len(renderable) else None
        grid = _grid(block)
        separator = (
            "\n"
            if grid is not None and following is not None and _grid(following) == grid
            else "\n\n"
        )

        if block.type is BlockType.HEADING:
            # Rendered for the reader, but deliberately not indexed: the
            # chunker excludes headings from chunks, so no citation can name
            # one and an anchor here would be unreachable.
            append("#" * _heading_level(block) + " ")
            append(block.text)
            append("\n\n")
            continue

        if block.type is BlockType.FIGURE and block.media_ref:
            # Picture first, then what it says underneath it. A rendered
            # diagram's description runs to several hundred characters, and as
            # alt text that is a wall of prose nobody reads inside a bracket.
            append(f"![]({block.media_ref})\n\n")
            start = length
            append(block.text)
            anchors[block.block_id] = (start, length)
            append("\n\n")
            continue

        prefix, suffix = _rendered(block)
        append(prefix)
        start = length
        append(block.text)
        anchors[block.block_id] = (start, length)
        append(suffix)
        append(separator)

    return "".join(parts), anchors


def markdown_keys(doc_id: str) -> tuple[str, str]:
    """Media keys for a document's markdown and its anchor index.

    Derived from `doc_id` alone so a reader can find them without a lookup.
    """
    return (
        media_key(doc_id=doc_id, kind="markdown", discriminator="document", extension="md"),
        media_key(doc_id=doc_id, kind="anchors", discriminator="document", extension="json"),
    )


def store(document: Document, blocks: list[Block], media_store: MediaStore) -> tuple[str, str]:
    """Render and persist the pair, returning the keys they were stored under."""
    markdown, anchors = render(document, blocks)
    markdown_ref, anchors_ref = markdown_keys(document.doc_id)
    media_store.put(markdown_ref, markdown.encode(), content_type="text/markdown")
    media_store.put(
        anchors_ref,
        json.dumps(anchors, ensure_ascii=False).encode(),
        content_type="application/json",
    )
    return markdown_ref, anchors_ref
