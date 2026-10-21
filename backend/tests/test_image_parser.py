from pathlib import Path

import pytest

from rag_ingestion.storage.media import LocalMediaStore
from rag_ingestion.models import BlockType, SourceType
from rag_ingestion.ingestion.parsers.datalab import content_type_for
from rag_ingestion.ingestion.parsers.image import parse_image
from rag_ingestion.ingestion.parsers.pdf import ExtractedPageBlock
from rag_ingestion.ingestion.pipeline import process_document
from rag_ingestion.ingestion.router import ParserKind, route_file


class _StubScanParser:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, list[int]]] = []

    def parse_pages(self, path: Path, pages: list[int]) -> list[ExtractedPageBlock]:
        self.calls.append((path, pages))
        return [
            ExtractedPageBlock(
                text="Lower block",
                page=1,
                bbox=(0.0, 40.0, 100.0, 60.0),
                confidence=0.72,
            ),
            ExtractedPageBlock(
                text="Upper heading",
                page=1,
                bbox=(0.0, 10.0, 100.0, 20.0),
                confidence=0.95,
                block_type=BlockType.HEADING,
            ),
        ]


@pytest.mark.parametrize(
    "suffix", [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"]
)
def test_image_extensions_route_to_ocr(suffix: str) -> None:
    route = route_file(Path(f"scan{suffix}"))

    assert route.parser is ParserKind.IMAGE_OCR
    assert route.source_type is SourceType.IMAGE
    assert route.requires_ocr is True


def test_content_type_is_derived_from_the_suffix() -> None:
    assert content_type_for(Path("a.pdf")) == "application/pdf"
    assert content_type_for(Path("a.PNG")) == "image/png"
    assert content_type_for(Path("a.unknown")) == "application/octet-stream"


def test_parse_image_orders_blocks_and_stores_the_original(tmp_path: Path) -> None:
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake-png-bytes")
    store = LocalMediaStore(tmp_path / "media")
    scan_parser = _StubScanParser()

    parsed = parse_image(
        image_path,
        owner_user_id="user-a",
        project_id="project-a",
        source_uri="upload://scan.png",
        scan_parser=scan_parser,
        media_store=store,
    )

    assert scan_parser.calls == [(image_path, [1])]
    assert parsed.document.source_type is SourceType.IMAGE
    assert [block.text for block in parsed.blocks] == ["Upper heading", "Lower block"]
    assert parsed.blocks[0].locators[0].page == 1
    assert parsed.blocks[1].low_confidence is True
    assert parsed.document.overall_confidence == pytest.approx(0.72)

    media_ref = parsed.blocks[0].media_ref
    assert media_ref is not None
    assert store.get(media_ref) == b"fake-png-bytes"


def test_parse_image_requires_a_scan_parser(tmp_path: Path) -> None:
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake-png-bytes")

    with pytest.raises(ValueError, match="scan parser is required"):
        parse_image(
            image_path,
            owner_user_id="user-a",
            project_id="project-a",
            source_uri="upload://scan.png",
        )


def test_pipeline_routes_images_through_the_scan_parser(tmp_path: Path) -> None:
    image_path = tmp_path / "scan.jpg"
    image_path.write_bytes(b"fake-jpeg-bytes")

    parsed = process_document(
        image_path,
        owner_user_id="user-a",
        project_id="project-a",
        scan_parser=_StubScanParser(),
    )

    assert parsed.document.source_type is SourceType.IMAGE
    assert len(parsed.blocks) == 2
