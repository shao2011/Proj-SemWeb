from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from enum import StrEnum

from pydantic import Field, model_validator

from rag_ingestion.config import ChunkingConfig
from rag_ingestion.models import Block, BlockType, Document, Locator, StrictModel
from rag_ingestion.chunking.structure import (
    NaturalUnit,
    StructureDetection,
    assign_clause_sections,
    detect_natural_unit,
)


class ChunkLevel(StrEnum):
    LEAF = "leaf"
    SECTION = "section"
    DOCUMENT = "document"


class Chunk(StrictModel):
    chunk_id: str
    doc_id: str
    owner_user_id: str
    project_id: str
    domain: str | None = None
    level: ChunkLevel
    source_text: str
    index_text: str
    source_block_ids: list[str]
    source_span: tuple[int, int] | None = None
    locators: list[Locator]
    confidence: float = Field(ge=0, le=1)
    language: str | None = None
    citable: bool
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    section_path: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    media_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class ChunkingResult(StrictModel):
    document: Document
    chunks: list[Chunk]
    natural_unit: NaturalUnit = NaturalUnit.NONE


_SPECIAL_TYPES = {
    BlockType.LIST,
    BlockType.TABLE_ROW,
    BlockType.FIGURE,
    BlockType.FORMULA,
    BlockType.CAPTION,
    BlockType.CODE,
    BlockType.SLIDE,
    BlockType.SPEECH_TURN,
}
_SENTENCE_PATTERN = re.compile(r".*?(?:[.!?](?=\s|$)|$)", re.DOTALL)


def _token_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _chunk_id(
    *,
    doc_id: str,
    level: ChunkLevel,
    source_block_ids: list[str],
    source_span: tuple[int, int] | None,
    source_text: str,
    section_path: list[str],
) -> str:
    identity = json.dumps(
        [doc_id, level.value, source_block_ids, source_span, source_text, section_path],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"chk_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _unique_locators(blocks: list[Block]) -> list[Locator]:
    locators: list[Locator] = []
    seen: set[str] = set()
    for block in blocks:
        for locator in block.locators:
            key = locator.model_dump_json()
            if key not in seen:
                seen.add(key)
                locators.append(locator)
    return locators


def _language(blocks: list[Block]) -> str | None:
    languages = [block.language for block in blocks if block.language]
    return max(set(languages), key=languages.count) if languages else None


def _breadcrumb(document: Document, section_path: list[str], blocks: list[Block]) -> str:
    parts = [document.title or document.doc_id, *section_path]
    pages = sorted(
        {
            locator.page
            for block in blocks
            for locator in block.locators
            if locator.kind == "page_bbox"
        }
    )
    if pages:
        page_label = str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"
        parts.append(f"page {page_label}")
    return " / ".join(parts)


def _sentence_spans(text: str) -> list[tuple[str, tuple[int, int]]]:
    spans: list[tuple[str, tuple[int, int]]] = []
    for match in _SENTENCE_PATTERN.finditer(text):
        raw = match.group()
        if not raw.strip():
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        start = match.start() + leading
        end = match.start() + trailing
        spans.append((text[start:end], (start, end)))
    return spans


def _split_oversized(block: Block, config: ChunkingConfig) -> list[tuple[str, tuple[int, int]]]:
    sentences = _sentence_spans(block.text)
    if not sentences:
        return [(block.text, (0, len(block.text)))]

    pieces: list[tuple[str, tuple[int, int]]] = []
    current: list[tuple[str, tuple[int, int]]] = []
    current_tokens = 0
    for sentence, span in sentences:
        sentence_tokens = _token_count(sentence)
        if current and current_tokens + sentence_tokens > config.leaf_max_tokens:
            pieces.append((" ".join(item[0] for item in current), (current[0][1][0], current[-1][1][1])))
            current = []
            current_tokens = 0
        if sentence_tokens > config.leaf_max_tokens:
            words = list(re.finditer(r"\S+", sentence))
            for offset in range(0, len(words), config.leaf_max_tokens):
                window = words[offset : offset + config.leaf_max_tokens]
                start = span[0] + window[0].start()
                end = span[0] + window[-1].end()
                pieces.append((block.text[start:end], (start, end)))
            continue
        current.append((sentence, span))
        current_tokens += sentence_tokens
    if current:
        pieces.append((" ".join(item[0] for item in current), (current[0][1][0], current[-1][1][1])))
    return pieces


def _make_leaf(
    document: Document,
    blocks: list[Block],
    *,
    source_text: str | None = None,
    source_span: tuple[int, int] | None = None,
    metadata: dict[str, object] | None = None,
) -> Chunk:
    text = source_text if source_text is not None else "\n\n".join(block.text for block in blocks)
    section_path = blocks[0].section_path.copy() if blocks else []
    source_block_ids = [block.block_id for block in blocks]
    breadcrumb = _breadcrumb(document, section_path, blocks)
    return Chunk(
        chunk_id=_chunk_id(
            doc_id=document.doc_id,
            level=ChunkLevel.LEAF,
            source_block_ids=source_block_ids,
            source_span=source_span,
            source_text=text,
            section_path=section_path,
        ),
        doc_id=document.doc_id,
        owner_user_id=document.owner_user_id,
        project_id=document.project_id,
        domain=document.domain,
        level=ChunkLevel.LEAF,
        source_text=text,
        index_text=f"{breadcrumb}\n{text}",
        source_block_ids=source_block_ids,
        source_span=source_span,
        locators=_unique_locators(blocks),
        confidence=min(block.confidence for block in blocks),
        language=_language(blocks),
        citable=True,
        section_path=section_path,
        refs=list(
            dict.fromkeys(
                relation.target_block_id
                for block in blocks
                for relation in block.relations
            )
        ),
        media_refs=list(
            dict.fromkeys(block.media_ref for block in blocks if block.media_ref)
        ),
        metadata=metadata or {},
    )


def _leaf_chunks(
    document: Document,
    blocks: list[Block],
    config: ChunkingConfig,
) -> list[Chunk]:
    def make(
        source_blocks: list[Block],
        *,
        source_text: str | None = None,
        source_span: tuple[int, int] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Chunk:
        return _make_leaf(
            document,
            source_blocks,
            source_text=source_text,
            source_span=source_span,
            metadata=metadata,
        )

    blocks_by_id = {block.block_id: block for block in blocks}
    row_parent_ids = {
        block.parent_block_id
        for block in blocks
        if block.type is BlockType.TABLE_ROW and block.parent_block_id
    }
    cell_parent_ids = {
        block.parent_block_id
        for block in blocks
        if block.type is BlockType.TABLE_CELL and block.parent_block_id
    }
    leaves: list[Chunk] = []
    prose_group: list[Block] = []
    prose_tokens = 0

    def flush_prose() -> None:
        nonlocal prose_group, prose_tokens
        if prose_group:
            previous = leaves[-1] if leaves else None
            can_merge_previous = (
                prose_tokens < config.leaf_min_tokens
                and previous is not None
                and not previous.metadata
                and previous.section_path == prose_group[0].section_path
                and _token_count(previous.source_text) + prose_tokens
                <= config.leaf_max_tokens
            )
            if can_merge_previous:
                previous_blocks = [
                    blocks_by_id[block_id] for block_id in previous.source_block_ids
                ]
                leaves[-1] = make([*previous_blocks, *prose_group])
            else:
                leaves.append(make(prose_group))
            prose_group = []
            prose_tokens = 0

    for block in sorted(blocks, key=lambda item: item.order):
        if block.type is BlockType.HEADING:
            flush_prose()
            continue
        if block.type is BlockType.TABLE and block.block_id in row_parent_ids:
            continue
        if block.type is BlockType.TABLE_CELL and block.parent_block_id in cell_parent_ids:
            continue
        if block.type in _SPECIAL_TYPES or block.type in {BlockType.TABLE, BlockType.TABLE_CELL}:
            flush_prose()
            if block.text.strip():
                leaves.append(make([block], metadata={"block_type": block.type.value}))
            continue
        if block.type is not BlockType.PARAGRAPH or not block.text.strip():
            continue

        block_tokens = _token_count(block.text)
        if block_tokens > config.leaf_max_tokens:
            flush_prose()
            for text, span in _split_oversized(block, config):
                leaves.append(
                    make(
                        [block],
                        source_text=text,
                        source_span=span,
                        metadata={"block_type": block.type.value},
                    )
                )
            continue

        section_changed = bool(prose_group) and prose_group[0].section_path != block.section_path
        target_exceeded = bool(prose_group) and prose_tokens + block_tokens > config.leaf_target_tokens
        if section_changed or target_exceeded:
            flush_prose()
        prose_group.append(block)
        prose_tokens += block_tokens
    flush_prose()
    return leaves


def _routing_chunk(
    document: Document,
    *,
    level: ChunkLevel,
    children: list[Chunk],
    section_path: list[str],
    parent_id: str | None,
) -> Chunk:
    source_text = "\n\n".join(child.source_text for child in children)
    source_block_ids = list(dict.fromkeys(block_id for child in children for block_id in child.source_block_ids))
    locators: list[Locator] = []
    seen: set[str] = set()
    for child in children:
        for locator in child.locators:
            key = locator.model_dump_json()
            if key not in seen:
                seen.add(key)
                locators.append(locator)
    breadcrumb = " / ".join([document.title or document.doc_id, *section_path])
    return Chunk(
        chunk_id=_chunk_id(
            doc_id=document.doc_id,
            level=level,
            source_block_ids=source_block_ids,
            source_span=None,
            source_text=source_text,
            section_path=section_path,
        ),
        doc_id=document.doc_id,
        owner_user_id=document.owner_user_id,
        project_id=document.project_id,
        domain=document.domain,
        level=level,
        source_text=source_text,
        index_text=f"{breadcrumb}\n{source_text}",
        source_block_ids=source_block_ids,
        locators=locators,
        confidence=min(child.confidence for child in children),
        language=next((child.language for child in children if child.language), None),
        citable=False,
        parent_id=parent_id,
        child_ids=[child.chunk_id for child in children],
        section_path=section_path,
        refs=list(dict.fromkeys(ref for child in children for ref in child.refs)),
        media_refs=list(
            dict.fromkeys(ref for child in children for ref in child.media_refs)
        ),
        metadata={"routing": True},
    )


def chunk_document(
    document: Document,
    blocks: list[Block],
    *,
    config: ChunkingConfig | None = None,
) -> ChunkingResult:
    active_config = config or ChunkingConfig()

    detection: StructureDetection = detect_natural_unit(blocks)
    structured_blocks = assign_clause_sections(blocks, detection)

    leaves = _leaf_chunks(document, structured_blocks, active_config)
    if not leaves:
        return ChunkingResult(
            document=document, chunks=[], natural_unit=detection.unit
        )

    grouped: dict[tuple[str, ...], list[Chunk]] = defaultdict(list)
    for leaf in leaves:
        grouped[tuple(leaf.section_path)].append(leaf)

    sections = [
        _routing_chunk(
            document,
            level=ChunkLevel.SECTION,
            children=section_leaves,
            section_path=list(section_key),
            parent_id=None,
        )
        for section_key, section_leaves in grouped.items()
    ]
    document_chunk = _routing_chunk(
        document,
        level=ChunkLevel.DOCUMENT,
        children=sections,
        section_path=[],
        parent_id=None,
    )
    for section, (_, section_leaves) in zip(sections, grouped.items(), strict=True):
        section.parent_id = document_chunk.chunk_id
        for leaf in section_leaves:
            leaf.parent_id = section.chunk_id

    return ChunkingResult(
        document=document,
        chunks=[*leaves, *sections, document_chunk],
        natural_unit=detection.unit,
    )
