import base64
import json
from pathlib import Path

import fitz
import httpx

import pytest

from rag_ingestion.models import PageBBoxLocator
from rag_ingestion.ingestion.parsers.datalab import DatalabClient
from rag_ingestion.ingestion.parsers.pdf import ExtractedPageBlock, PageKind, parse_pdf, probe_pdf

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeScanParser:
    def __init__(self) -> None:
        self.pages: list[int] = []

    def parse_pages(self, path: Path, pages: list[int]) -> list[ExtractedPageBlock]:
        self.pages = pages
        return [
            ExtractedPageBlock(
                text="Scanned page text",
                page=pages[0],
                bbox=(5, 10, 200, 50),
                confidence=0.82,
            )
        ]


def _mixed_pdf(path: Path) -> None:
    document = fitz.open()
    native_page = document.new_page(width=600, height=400)
    native_page.insert_text(
        (20, 40),
        "This born-digital page has enough text to pass the native text-layer probe.",
    )
    scanned_page = document.new_page(width=600, height=400)
    scanned_page.insert_image(scanned_page.rect, stream=_ONE_PIXEL_PNG)
    document.save(path)
    document.close()


def test_probe_pdf_raises_value_error_on_a_file_that_is_not_a_pdf(tmp_path: Path) -> None:
    """pymupdf raises its own `FzErrorBase` family on a malformed file, not
    `ValueError`. The upload route only catches `ValueError` for a bad
    document, so an untranslated exception here would 500 instead of 422 and
    leave the file's status stuck at "processing" forever."""
    path = tmp_path / "not-a-pdf.pdf"
    path.write_bytes(b"not actually a pdf file")

    with pytest.raises(ValueError, match="Could not open PDF"):
        probe_pdf(path)


def test_pdf_routes_only_scanned_pages_to_scan_parser(tmp_path: Path) -> None:
    path = tmp_path / "mixed.pdf"
    _mixed_pdf(path)
    scan_parser = FakeScanParser()

    probes = probe_pdf(path)
    result = parse_pdf(
        path,
        owner_user_id="user-a",
        project_id="project-a",
        source_uri="upload://mixed.pdf",
        scan_parser=scan_parser,
    )

    assert [probe.kind for probe in probes] == [PageKind.BORN_DIGITAL, PageKind.SCANNED]
    assert scan_parser.pages == [2]
    assert [block.text for block in result.blocks] == [
        "This born-digital page has enough text to pass the native text-layer probe.",
        "Scanned page text",
    ]
    assert result.blocks[0].confidence == 1.0
    assert result.blocks[1].confidence == 0.82
    assert result.blocks[1].locators == [
        PageBBoxLocator(page=2, bbox=(5, 10, 200, 50))
    ]


def test_datalab_uses_convert_endpoint_and_maps_json_blocks(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-test")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            body = request.read()
            assert request.url.path == "/api/v1/convert"
            assert request.headers["X-API-Key"] == "secret"
            assert b'name="page_range"' in body
            assert b"\r\n\r\n1\r\n" in body
            assert b'name="output_format"' in body
            assert b"\r\n\r\njson\r\n" in body
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "request_id": "req-1",
                    "request_check_url": "https://www.datalab.to/api/v1/convert/req-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "success": True,
                "json": json.dumps(
                    {
                        "children": [
                            {
                                "block_type": "Text",
                                "text": "OCR result",
                                "page": 1,
                                "bbox": [10, 20, 30, 40],
                                "confidence": 0.91,
                            }
                        ]
                    }
                ),
            },
        )

    client = DatalabClient(
        api_keys=["secret"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    blocks = client.parse_pages(path, pages=[2])

    assert len(requests) == 2
    assert requests[1].url.path == "/api/v1/convert/req-1"
    assert blocks == [
        ExtractedPageBlock(
            text="OCR result",
            page=2,
            bbox=(10, 20, 30, 40),
            confidence=0.91,
        )
    ]


def test_datalab_reuses_one_key_across_submit_and_poll(tmp_path: Path) -> None:
    """A job's submit and its poll must land on the same account: the check
    URL is almost certainly scoped to whichever account created the job."""
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-test")
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["X-API-Key"])
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "request_check_url": "https://www.datalab.to/api/v1/convert/req-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "success": True,
                "json": json.dumps({"children": []}),
            },
        )

    client = DatalabClient(
        api_keys=["key-a", "key-b"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.parse_pages(path, pages=[1])

    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]


def test_datalab_round_robins_across_separate_parse_calls(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-test")
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["X-API-Key"])
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "request_check_url": "https://www.datalab.to/api/v1/convert/req-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "complete",
                "success": True,
                "json": json.dumps({"children": []}),
            },
        )

    client = DatalabClient(
        api_keys=["key-a", "key-b"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.parse_pages(path, pages=[1])
    client.parse_pages(path, pages=[1])

    # Each call's submit+poll pair stays on one key; the two calls alternate.
    assert seen_keys == ["key-a", "key-a", "key-b", "key-b"]


def test_datalab_rejects_an_empty_key_list() -> None:
    with pytest.raises(ValueError):
        DatalabClient(api_keys=[])
