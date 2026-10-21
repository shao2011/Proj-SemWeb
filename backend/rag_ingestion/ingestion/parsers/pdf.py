from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import fitz

from rag_ingestion.storage.media import MediaStore, media_key
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    PageBBoxLocator,
    ParseResult,
    SourceType,
)

PAGE_RENDER_DPI = 150


class PageKind(StrEnum):
    BORN_DIGITAL = "born_digital"
    SCANNED = "scanned"


@dataclass(frozen=True, slots=True)
class PageProbe:
    page: int
    kind: PageKind
    character_count: int
    has_fonts: bool
    image_area_ratio: float


@dataclass(frozen=True, slots=True)
class ExtractedPageBlock:
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    confidence: float
    block_type: BlockType = BlockType.PARAGRAPH


class ScanParser(Protocol):
    def parse_pages(self, path: Path, pages: list[int]) -> list[ExtractedPageBlock]: ...


def _open_pdf(path: Path) -> fitz.Document:
    """Opened from memory, not by path.

    Two things ride on this: `fitz.open(path)` raises its own `FzErrorBase`
    family, not `ValueError`, on a file that is not a real PDF, so callers
    already expecting `ValueError` for a bad document need it translated. And
    opening by path keeps mupdf holding its own OS file handle on `path` for
    as long as the document is open, including on that same failure -- which
    on Windows collides with the caller's own cleanup of that path right
    after, turning a clean 422 into an unhandled 500 from the cleanup itself.
    """
    try:
        return fitz.open(stream=path.read_bytes(), filetype="pdf")
    except (fitz.FileDataError, fitz.EmptyFileError, fitz.mupdf.FzErrorBase) as error:
        raise ValueError(f"Could not open PDF: {error}") from error


def _image_area_ratio(page: fitz.Page) -> float:
    page_area = max(page.rect.width * page.rect.height, 1.0)
    image_area = 0.0
    for image in page.get_images(full=True):
        for rectangle in page.get_image_rects(image[0]):
            image_area += rectangle.width * rectangle.height
    return min(image_area / page_area, 1.0)


def probe_pdf(path: Path, *, digital_text_threshold: int = 20) -> list[PageProbe]:
    probes: list[PageProbe] = []
    with _open_pdf(path) as pdf:
        for page_index, page in enumerate(pdf):
            text = page.get_text("text").strip()
            character_count = len(text)
            has_fonts = bool(page.get_fonts())
            image_area_ratio = _image_area_ratio(page)
            is_digital = character_count >= digital_text_threshold or (
                character_count > 0 and has_fonts
            )
            probes.append(
                PageProbe(
                    page=page_index + 1,
                    kind=PageKind.BORN_DIGITAL if is_digital else PageKind.SCANNED,
                    character_count=character_count,
                    has_fonts=has_fonts,
                    image_area_ratio=image_area_ratio,
                )
            )
    return probes


def _native_blocks(path: Path, pages: set[int]) -> list[ExtractedPageBlock]:
    extracted: list[ExtractedPageBlock] = []
    with _open_pdf(path) as pdf:
        for page_number in sorted(pages):
            page = pdf[page_number - 1]
            for raw_block in page.get_text("blocks", sort=True):
                text = raw_block[4].strip()
                if not text:
                    continue
                extracted.append(
                    ExtractedPageBlock(
                        text=text,
                        page=page_number,
                        bbox=tuple(float(value) for value in raw_block[:4]),
                        confidence=1.0,
                    )
                )
    return extracted


def render_pages(
    path: Path,
    pages: list[int],
    *,
    doc_id: str,
    media_store: MediaStore,
    dpi: int = PAGE_RENDER_DPI,
) -> dict[int, str]:
    """Render selected pages to PNG and return a page number to media key map.

    Only the pages that actually need visual evidence are rendered; born-digital
    pages carry reliable text and would only cost storage.
    """
    references: dict[int, str] = {}
    if not pages:
        return references
    with _open_pdf(path) as pdf:
        for page_number in sorted(set(pages)):
            pixmap = pdf[page_number - 1].get_pixmap(dpi=dpi)
            key = media_key(
                doc_id=doc_id,
                kind="page",
                discriminator=str(page_number),
                extension="png",
            )
            references[page_number] = media_store.put(
                key, pixmap.tobytes("png"), content_type="image/png"
            )
    return references


def parse_pdf(
    path: Path,
    *,
    owner_user_id: str,
    project_id: str,
    source_uri: str,
    scan_parser: ScanParser | None = None,
    media_store: MediaStore | None = None,
) -> ParseResult:
    document = Document.from_content(
        content=path.read_bytes(),
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.PDF,
        source_uri=source_uri,
        title=path.stem,
        parse_engine="pymupdf+datalab" if scan_parser else "pymupdf",
        parse_version="1",
    )
    probes = probe_pdf(path)
    digital_pages = {probe.page for probe in probes if probe.kind is PageKind.BORN_DIGITAL}
    scanned_pages = [probe.page for probe in probes if probe.kind is PageKind.SCANNED]
    if scanned_pages and scan_parser is None:
        raise ValueError("A scan parser is required for scanned PDF pages")

    extracted = _native_blocks(path, digital_pages)
    if scanned_pages and scan_parser is not None:
        extracted.extend(scan_parser.parse_pages(path, scanned_pages))
    # A block on a page this document does not have cannot be placed, and
    # `probes[item.page - 1]` below is what it used to reach: an out-of-range
    # page from an OCR response took the whole file down with an `IndexError`
    # rather than being reported as the bad block it is.
    probe_by_page = {probe.page: probe for probe in probes}
    stray = sorted({item.page for item in extracted} - probe_by_page.keys())
    if stray:
        raise ValueError(
            f"Parsed blocks land on page(s) {stray}, which are outside this "
            f"{len(probes)}-page document"
        )
    extracted.sort(key=lambda item: (item.page, item.bbox[1], item.bbox[0]))

    page_media: dict[int, str] = {}
    if media_store is not None:
        page_media = render_pages(
            path,
            scanned_pages,
            doc_id=document.doc_id,
            media_store=media_store,
        )

    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=order,
            type=item.block_type,
            text=item.text,
            confidence=item.confidence,
            low_confidence=item.confidence < 0.8,
            locators=[PageBBoxLocator(page=item.page, bbox=item.bbox)],
            media_ref=page_media.get(item.page),
            metadata={"page_kind": probe_by_page[item.page].kind.value},
        )
        for order, item in enumerate(extracted)
    ]
    # `default=1.0` said a document nothing could be read from was perfectly
    # reliable, which is the opposite of what happened and made "extracted
    # nothing" invisible to every metric and screen downstream.
    if not blocks:
        raise ValueError("No text could be extracted from this PDF")
    document.overall_confidence = min(block.confidence for block in blocks)
    return ParseResult(document=document, blocks=blocks)
