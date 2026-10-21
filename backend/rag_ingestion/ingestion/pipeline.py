from pathlib import Path

from rag_ingestion.storage.media import MediaStore
from rag_ingestion.models import ParseResult
from rag_ingestion.ingestion.parsers.datalab import DocumentConverter
from rag_ingestion.ingestion.parsers.image import parse_image
from rag_ingestion.ingestion.parsers.office import parse_docx, parse_pptx, parse_xlsx
from rag_ingestion.ingestion.parsers.pdf import ScanParser, parse_pdf
from rag_ingestion.language import detect_language
from rag_ingestion.ingestion.reconciliation import reconcile_blocks
from rag_ingestion.ingestion.router import ParserKind, route_file


def process_document(
    path: Path,
    *,
    owner_user_id: str,
    project_id: str,
    source_uri: str | None = None,
    scan_parser: ScanParser | None = None,
    media_store: MediaStore | None = None,
    converter: DocumentConverter | None = None,
) -> ParseResult:
    resolved_path = path.resolve()
    uri = source_uri or resolved_path.as_uri()
    route = route_file(resolved_path)
    if route.parser is ParserKind.DOCX_NATIVE:
        parsed = parse_docx(
            resolved_path,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_uri=uri,
        )
    elif route.parser is ParserKind.XLSX_NATIVE:
        parsed = parse_xlsx(
            resolved_path,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_uri=uri,
            media_store=media_store,
        )
    elif route.parser is ParserKind.PPTX_NATIVE:
        parsed = parse_pptx(
            resolved_path,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_uri=uri,
            media_store=media_store,
            converter=converter,
        )
    elif route.parser is ParserKind.IMAGE_OCR:
        parsed = parse_image(
            resolved_path,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_uri=uri,
            scan_parser=scan_parser,
            media_store=media_store,
        )
    else:
        parsed = parse_pdf(
            resolved_path,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_uri=uri,
            scan_parser=scan_parser,
            media_store=media_store,
        )
    blocks = [
        block
        if block.language
        else block.model_copy(update={"language": detect_language(block.text)})
        for block in reconcile_blocks(parsed.blocks)
    ]
    # Nothing to chunk means nothing to index, and the upload would otherwise
    # report success and leave a document that answers no question ever asked
    # of it. The image parser refused this on its own; every other format
    # accepted it silently.
    if not any(block.text.strip() for block in blocks):
        raise ValueError("No text could be extracted from this document")
    return ParseResult(document=parsed.document, blocks=blocks)
