from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rag_ingestion.models import SourceType


class ParserKind(StrEnum):
    PDF = "pdf"
    DOCX_NATIVE = "docx_native"
    XLSX_NATIVE = "xlsx_native"
    PPTX_NATIVE = "pptx_native"
    IMAGE_OCR = "image_ocr"


@dataclass(frozen=True, slots=True)
class ParseRoute:
    source_type: SourceType
    parser: ParserKind
    requires_ocr: bool = False


_IMAGE_ROUTE = ParseRoute(SourceType.IMAGE, ParserKind.IMAGE_OCR, requires_ocr=True)

_ROUTES = {
    ".pdf": ParseRoute(SourceType.PDF, ParserKind.PDF),
    ".docx": ParseRoute(SourceType.DOCX, ParserKind.DOCX_NATIVE),
    ".xlsx": ParseRoute(SourceType.XLSX, ParserKind.XLSX_NATIVE),
    ".pptx": ParseRoute(SourceType.PPTX, ParserKind.PPTX_NATIVE),
    ".png": _IMAGE_ROUTE,
    ".jpg": _IMAGE_ROUTE,
    ".jpeg": _IMAGE_ROUTE,
    ".tif": _IMAGE_ROUTE,
    ".tiff": _IMAGE_ROUTE,
    ".webp": _IMAGE_ROUTE,
    ".bmp": _IMAGE_ROUTE,
}


def route_file(path: Path) -> ParseRoute:
    try:
        return _ROUTES[path.suffix.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported file type: {path.suffix or '<none>'}") from error
