from pathlib import Path

import pytest
from pydantic import ValidationError

from rag_ingestion.models import (
    Block,
    BlockType,
    CellRangeLocator,
    Document,
    PageBBoxLocator,
    SourceType,
)
from rag_ingestion.ingestion.router import ParserKind, route_file


def test_document_and_block_ids_are_deterministic() -> None:
    content = b"same document bytes"

    first_document = Document.from_content(
        content=content,
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.PDF,
        source_uri="upload://policy.pdf",
        parse_engine="native",
        parse_version="1",
    )
    second_document = Document.from_content(
        content=content,
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.PDF,
        source_uri="upload://policy.pdf",
        parse_engine="native",
        parse_version="1",
    )
    first_block = Block.from_source(
        doc_id=first_document.doc_id,
        order=0,
        type=BlockType.PARAGRAPH,
        text="Noi dung goc",
        confidence=1.0,
        locators=[PageBBoxLocator(page=1, bbox=(10, 20, 30, 40))],
    )
    second_block = Block.from_source(
        doc_id=second_document.doc_id,
        order=0,
        type=BlockType.PARAGRAPH,
        text="Noi dung goc",
        confidence=1.0,
        locators=[PageBBoxLocator(page=1, bbox=(10, 20, 30, 40))],
    )

    assert first_document.doc_id == second_document.doc_id
    assert first_document.content_hash == second_document.content_hash
    assert first_block.block_id == second_block.block_id


def test_document_identity_is_scoped_to_owner_and_project() -> None:
    common = {
        "content": b"same document bytes",
        "source_type": SourceType.PDF,
        "source_uri": "upload://policy.pdf",
        "parse_engine": "native",
        "parse_version": "1",
    }

    first = Document.from_content(
        owner_user_id="user-a", project_id="project-a", **common
    )
    other_owner = Document.from_content(
        owner_user_id="user-b", project_id="project-a", **common
    )
    other_project = Document.from_content(
        owner_user_id="user-a", project_id="project-b", **common
    )

    assert len({first.doc_id, other_owner.doc_id, other_project.doc_id}) == 3


def test_locator_union_rejects_modality_fields_from_another_kind() -> None:
    with pytest.raises(ValidationError):
        PageBBoxLocator.model_validate(
            {"kind": "page_bbox", "page": 1, "bbox": [0, 0, 1, 1], "sheet": "Sheet1"}
        )


def test_block_requires_at_least_one_locator() -> None:
    with pytest.raises(ValidationError):
        Block.from_source(
            doc_id="doc-test",
            order=0,
            type=BlockType.PARAGRAPH,
            text="Missing provenance",
            confidence=1.0,
            locators=[],
        )


@pytest.mark.parametrize(
    ("filename", "expected_source", "expected_parser"),
    [
        ("policy.PDF", SourceType.PDF, ParserKind.PDF),
        ("memo.docx", SourceType.DOCX, ParserKind.DOCX_NATIVE),
        ("ledger.xlsx", SourceType.XLSX, ParserKind.XLSX_NATIVE),
        ("briefing.pptx", SourceType.PPTX, ParserKind.PPTX_NATIVE),
    ],
)
def test_router_selects_native_office_parsers(
    filename: str, expected_source: SourceType, expected_parser: ParserKind
) -> None:
    route = route_file(Path(filename))

    assert route.source_type is expected_source
    assert route.parser is expected_parser
    assert route.requires_ocr is False


def test_router_rejects_unsupported_files() -> None:
    with pytest.raises(ValueError, match="Unsupported file type"):
        route_file(Path("archive.zip"))


def test_cell_locator_normalizes_excel_range() -> None:
    locator = CellRangeLocator(sheet="Data", range="b2:d4")

    assert locator.range == "B2:D4"
