from __future__ import annotations

from pathlib import Path

from rag_ingestion.storage.media import MediaStore, media_key
from rag_ingestion.models import (
    Block,
    Document,
    PageBBoxLocator,
    ParseResult,
    SourceType,
)
from rag_ingestion.ingestion.parsers.datalab import content_type_for
from rag_ingestion.ingestion.parsers.pdf import ScanParser

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}
)


def parse_image(
    path: Path,
    *,
    owner_user_id: str,
    project_id: str,
    source_uri: str,
    scan_parser: ScanParser | None = None,
    media_store: MediaStore | None = None,
) -> ParseResult:
    """Parse a standalone image through the same OCR path as a scanned PDF page."""
    if scan_parser is None:
        raise ValueError("A scan parser is required for image sources")

    content = path.read_bytes()
    document = Document.from_content(
        content=content,
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.IMAGE,
        source_uri=source_uri,
        title=path.stem,
        parse_engine="datalab",
        parse_version="1",
    )

    stored_ref: str | None = None
    if media_store is not None:
        key = media_key(
            doc_id=document.doc_id,
            kind="page",
            discriminator="1",
            extension=path.suffix or ".bin",
        )
        stored_ref = media_store.put(
            key, content, content_type=content_type_for(path)
        )

    extracted = scan_parser.parse_pages(path, [1])
    extracted.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=order,
            type=item.block_type,
            text=item.text,
            confidence=item.confidence,
            low_confidence=item.confidence < 0.8,
            locators=[PageBBoxLocator(page=1, bbox=item.bbox)],
            media_ref=stored_ref,
            metadata={"page_kind": "scanned"},
        )
        for order, item in enumerate(extracted)
    ]
    # `default=1.0` reported an image OCR read nothing from as perfectly
    # reliable, so "no text here" and "every word certain" were the same value.
    if not blocks:
        raise ValueError("No text could be extracted from this image")
    document.overall_confidence = min(block.confidence for block in blocks)
    return ParseResult(document=document, blocks=blocks)
