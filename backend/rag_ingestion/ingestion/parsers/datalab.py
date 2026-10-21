from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import httpx

from rag_ingestion.logger import get_logger
from rag_ingestion.models import BlockType
from rag_ingestion.ingestion.parsers.pdf import ExtractedPageBlock
from rag_ingestion.providers.keys import KeyRotator
from rag_ingestion.providers.registry import ProviderResponseError

_log = get_logger(__name__)


def _object(value: Any, what: str) -> dict[str, Any]:
    """Insist a payload is a JSON object before anything reads fields off it.

    Every `.get()` below assumed one. A response that is valid JSON but the wrong
    shape, a list or a bare string, raised `AttributeError` from somewhere deep in
    the walk, which is indistinguishable from a bug in this file.
    """
    if not isinstance(value, dict):
        raise ProviderResponseError(
            f"Datalab {what} is {type(value).__name__}, expected a JSON object"
        )
    return value


def _finite(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProviderResponseError(
            f"Datalab {what} is not a number: {value!r}"
        ) from error
    if number != number or number in (float("inf"), float("-inf")):
        raise ProviderResponseError(f"Datalab {what} is not finite: {value!r}")
    return number


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if text := data.strip():
            self.parts.append(text)


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(parser.parts)


_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    # Sent as octet-stream a deck is refused outright: "Invalid file type. Only
    # PDF files, word documents, spreadsheets, powerpoints, HTML, and PNG, JPG,
    # GIF, TIFF, and WEBP images are accepted."
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


_BLOCK_TYPES = {
    "sectionheader": BlockType.HEADING,
    "text": BlockType.PARAGRAPH,
    "listitem": BlockType.LIST,
    "table": BlockType.TABLE,
    "figure": BlockType.FIGURE,
    "equation": BlockType.FORMULA,
    "caption": BlockType.CAPTION,
    "code": BlockType.CODE,
}


@dataclass(frozen=True, slots=True)
class ConvertedBlock:
    """One block of a document the service rendered and read for us.

    Unlike `ExtractedPageBlock`, which describes a region of a page a scan
    parser was asked about, this carries the picture itself: a slide's diagram
    is often drawn as shapes, so the rendered image exists nowhere in the
    uploaded file and arrives only here.
    """

    text: str
    page: int
    bbox: tuple[float, float, float, float]
    block_type: BlockType
    media: bytes | None = None
    media_name: str | None = None
    furniture: bool = False


class DocumentConverter(Protocol):
    def convert(self, path: Path) -> list[ConvertedBlock]: ...


#: Datalab's own block names, normalized by `re_normalize_type`.
_CONVERTED_TYPES = {
    "sectionheader": BlockType.HEADING,
    "title": BlockType.HEADING,
    "text": BlockType.PARAGRAPH,
    "textinlinemath": BlockType.PARAGRAPH,
    "listitem": BlockType.LIST,
    "listgroup": BlockType.LIST,
    "table": BlockType.TABLE,
    "tablegroup": BlockType.TABLE,
    "figure": BlockType.FIGURE,
    "figuregroup": BlockType.FIGURE,
    "picture": BlockType.FIGURE,
    "picturegroup": BlockType.FIGURE,
    "equation": BlockType.FORMULA,
    "caption": BlockType.CAPTION,
    "code": BlockType.CODE,
}

#: Repeated on every page and never content: a slide number, a running footer.
_FURNITURE_TYPES = frozenset({"pageheader", "pagefooter", "footnote"})


class DatalabClient:
    def __init__(
        self,
        *,
        api_keys: list[str],
        client: httpx.Client | None = None,
        base_url: str = "https://www.datalab.to/api/v1/convert",
        timeout: float = 120.0,
        mode: str = "accurate",
        poll_interval: float = 0.25,
        max_polls: int = 120,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout)
        self._base_url = base_url
        self._rotator = KeyRotator(api_keys)
        self._mode = mode
        self._poll_interval = poll_interval
        self._max_polls = max_polls

    def parse_pages(self, path: Path, pages: list[int]) -> list[ExtractedPageBlock]:
        if not pages:
            return []
        # One key for the whole job: the check URL a submit returns is almost
        # certainly scoped to whichever account created it, so the poll and
        # the result fetch below must reuse it rather than drawing again.
        headers = {"X-API-Key": self._rotator.next_key()}
        page_range = ",".join(str(page - 1) for page in pages)
        with path.open("rb") as source:
            response = self._client.post(
                self._base_url,
                headers=headers,
                data={
                    "mode": self._mode,
                    "page_range": page_range,
                    "output_format": "json",
                    "disable_image_captions": "true",
                    "skip_cache": "false",
                },
                files={"file": (path.name, source, content_type_for(path))},
            )
        response.raise_for_status()
        initial = _object(response.json(), "submit response")
        if not initial.get("success", True):
            raise RuntimeError(initial.get("error") or "Datalab rejected the conversion")
        check_url = initial.get("request_check_url")
        if not isinstance(check_url, str) or not check_url:
            raise ProviderResponseError(
                "Datalab accepted the conversion without a request_check_url"
            )
        final = self._poll(check_url, headers)
        payload = self._result_payload(final, headers)
        page_map = {page - 1: page for page in pages}
        return list(self._walk_blocks(payload, page_map))

    def convert(self, path: Path) -> list[ConvertedBlock]:
        """Render a whole document and read every page of it.

        Image captions are left on. They cost nothing extra -- measured at the
        same 1 cent per page either way -- and they are the only thing that
        makes a diagram searchable: with them off, every Figure block comes
        back with empty text and the deck's architecture is invisible.
        """
        headers = {"X-API-Key": self._rotator.next_key()}
        with path.open("rb") as source:
            response = self._client.post(
                self._base_url,
                headers=headers,
                data={
                    "mode": self._mode,
                    "output_format": "json",
                    "disable_image_captions": "false",
                    "skip_cache": "false",
                },
                files={"file": (path.name, source, content_type_for(path))},
            )
        response.raise_for_status()
        initial = _object(response.json(), "submit response")
        if not initial.get("success", True):
            raise RuntimeError(initial.get("error") or "Datalab rejected the conversion")
        check_url = initial.get("request_check_url")
        if not isinstance(check_url, str) or not check_url:
            raise ProviderResponseError(
                "Datalab accepted the conversion without a request_check_url"
            )
        final = self._poll(check_url, headers)
        return _converted_blocks(self._result_payload(final, headers))

    def _poll(self, check_url: str, headers: dict[str, str]) -> dict[str, Any]:
        for poll_number in range(self._max_polls):
            response = self._client.get(check_url, headers=headers)
            response.raise_for_status()
            payload = _object(response.json(), "poll response")
            status = str(payload.get("status", "")).lower()
            if status in {"complete", "completed"}:
                if not payload.get("success", True):
                    raise RuntimeError(payload.get("error") or "Datalab conversion failed")
                return payload
            if status in {"failed", "error"}:
                raise RuntimeError(payload.get("error") or "Datalab conversion failed")
            if poll_number + 1 < self._max_polls:
                time.sleep(self._poll_interval)
        raise TimeoutError("Datalab conversion did not complete within the polling limit")

    def _result_payload(self, final: dict[str, Any], headers: dict[str, str]) -> Any:
        payload = final.get("json")
        if payload is None and final.get("result_url"):
            response = self._client.get(final["result_url"], headers=headers)
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError as error:
                raise ProviderResponseError(
                    f"Datalab result is not valid JSON: {error}"
                ) from error
        if payload is None:
            raise RuntimeError("Datalab completed without JSON output")
        return payload

    def _walk_blocks(
        self, value: Any, page_map: dict[int, int]
    ) -> list[ExtractedPageBlock]:
        blocks: list[ExtractedPageBlock] = []
        if isinstance(value, list):
            for item in value:
                blocks.extend(self._walk_blocks(item, page_map))
            return blocks
        if not isinstance(value, dict):
            return blocks

        children = value.get("children") or value.get("blocks")
        if children:
            blocks.extend(self._walk_blocks(children, page_map))
            return blocks

        raw_text = value.get("text") or value.get("html") or value.get("markdown")
        bbox = value.get("bbox")
        raw_page = value.get("page", value.get("page_id"))
        if (
            raw_text
            and isinstance(bbox, (list, tuple))
            and len(bbox) == 4
            and raw_page is not None
        ):
            try:
                page_id = int(raw_page)
            except (TypeError, ValueError) as error:
                raise ProviderResponseError(
                    f"Datalab block page is not an integer: {raw_page!r}"
                ) from error
            page = page_map.get(page_id)
            if page is None:
                # Only pages this call asked for are accepted. The old fallback
                # guessed `page_id + 1`, which on a response numbered differently
                # than expected produced a page number the document may not even
                # have, and `parse_pdf` then indexed its probe list with it.
                _log.warning(
                    "Datalab returned a block on page id %r, which was not "
                    "requested (%s); skipping it",
                    page_id,
                    sorted(page_map),
                )
                return blocks
            x0, y0, x1, y1 = (
                _finite(item, f"bbox element {position}")
                for position, item in enumerate(bbox)
            )
            normalized_type = re_normalize_type(str(value.get("block_type") or value.get("type") or "text"))
            blocks.append(
                ExtractedPageBlock(
                    text=_plain_text(str(raw_text)),
                    page=page,
                    # Normalized rather than passed through: corner order is a
                    # coordinate convention, not a provider fault, and the locator
                    # contract requires the low corner first.
                    bbox=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                    confidence=_finite(value.get("confidence", 0.8), "confidence"),
                    block_type=_BLOCK_TYPES.get(normalized_type, BlockType.PARAGRAPH),
                )
            )
            return blocks

        for child in value.values():
            if isinstance(child, (dict, list)):
                blocks.extend(self._walk_blocks(child, page_map))
        return blocks


def re_normalize_type(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _converted_blocks(payload: Any) -> list[ConvertedBlock]:
    """Flatten the response into blocks, keeping each one's own picture.

    Pages are numbered from zero in the response and from one everywhere in
    this codebase; converting here rather than at each use keeps a citation
    from landing one slide off.
    """
    blocks: list[ConvertedBlock] = []
    for leaf in _leaves(payload):
        raw_page = leaf.get("page", leaf.get("page_id"))
        bbox = leaf.get("bbox")
        if raw_page is None or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            page = int(raw_page) + 1
        except (TypeError, ValueError) as error:
            raise ProviderResponseError(
                f"Datalab block page is not an integer: {raw_page!r}"
            ) from error

        name = re_normalize_type(str(leaf.get("block_type") or leaf.get("type") or "text"))
        media_name, media = _first_image(leaf.get("images"))
        text = _plain_text(str(leaf.get("html") or leaf.get("text") or leaf.get("markdown") or ""))
        if not text and media is None:
            continue

        x0, y0, x1, y1 = (
            _finite(item, f"bbox element {position}")
            for position, item in enumerate(bbox)
        )
        blocks.append(
            ConvertedBlock(
                text=text,
                page=page,
                bbox=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                block_type=_CONVERTED_TYPES.get(name, BlockType.PARAGRAPH),
                media=media,
                media_name=media_name,
                furniture=name in _FURNITURE_TYPES,
            )
        )
    return blocks


def _leaves(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaves(item)]
    if not isinstance(value, dict):
        return []
    if children := (value.get("children") or value.get("blocks")):
        return _leaves(children)
    return [value]


def _first_image(images: Any) -> tuple[str | None, bytes | None]:
    """The picture a block carries, decoded.

    A block holds its own images inline as base64, keyed by file name, so
    nothing has to be matched up against a document-level image list.
    """
    if not isinstance(images, dict):
        return None, None
    for name, payload in images.items():
        if not isinstance(payload, str):
            continue
        try:
            return str(name), base64.b64decode(payload)
        except (ValueError, binascii.Error):
            _log.warning("Datalab image %r is not valid base64; skipping it", name)
    return None, None
