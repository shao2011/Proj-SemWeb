from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from rag_ingestion.models import BlockType, SourceType
from rag_ingestion.ingestion.pipeline import process_document


def test_process_document_routes_parses_and_reconciles_docx(tmp_path: Path) -> None:
    path = tmp_path / "memo.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Scope</w:t></w:r></w:p>
                    <w:p><w:r><w:t>This policy applies globally.</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """,
        )

    result = process_document(
        path,
        owner_user_id="user-a",
        project_id="project-a",
    )

    assert result.document.source_type is SourceType.DOCX
    assert result.document.source_uri == path.resolve().as_uri()
    assert [block.type for block in result.blocks] == [
        BlockType.HEADING,
        BlockType.PARAGRAPH,
    ]
    assert result.blocks[1].parent_block_id == result.blocks[0].block_id


def test_a_document_with_nothing_to_read_is_refused(tmp_path: Path) -> None:
    """Otherwise the upload reports success and indexes zero chunks.

    An image parser already refused this case on its own; every other format
    accepted the file and left the user with a document that answers nothing.
    """
    path = tmp_path / "blank.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>   </w:t></w:r></w:p></w:body>
                </w:document>
            """,
        )

    with pytest.raises(ValueError, match="No text could be extracted"):
        process_document(path, owner_user_id="user-a", project_id="project-a")


def test_an_empty_spreadsheet_is_refused_too(tmp_path: Path) -> None:
    path = tmp_path / "blank.xlsx"
    Workbook().save(path)

    with pytest.raises(ValueError, match="No text could be extracted"):
        process_document(path, owner_user_id="user-a", project_id="project-a")


def test_process_document_labels_block_language(tmp_path) -> None:
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    from rag_ingestion.ingestion.pipeline import process_document

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            """
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body>
                <w:p><w:r><w:t>Travel requests require written approval</w:t></w:r></w:p>
                <w:p><w:r><w:t>ผู้ประกอบการต้องยื่นแบบแสดงรายการภาษี</w:t></w:r></w:p>
              </w:body>
            </w:document>
            """,
        )
    path = tmp_path / "mixed.docx"
    path.write_bytes(buffer.getvalue())

    parsed = process_document(path, owner_user_id="user-a", project_id="project-a")

    languages = [block.language for block in parsed.blocks]
    assert "en" in languages
    assert "th" in languages
