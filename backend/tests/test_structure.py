from rag_ingestion.models import Block, BlockType, Document, OfficePathLocator, SourceType
from rag_ingestion.chunking.structure import (
    NaturalUnit,
    assign_clause_sections,
    detect_natural_unit,
)


def _document() -> Document:
    return Document.from_content(
        content=b"structure fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://fixture.docx",
        parse_engine="native",
        parse_version="1",
    )


def _blocks(document: Document, texts: list[str]) -> list[Block]:
    return [
        Block.from_source(
            doc_id=document.doc_id,
            order=order,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[
                OfficePathLocator(part="word/document.xml", path=f"/p[{order + 1}]")
            ],
        )
        for order, text in enumerate(texts)
    ]


def test_detects_numeric_clause_structure() -> None:
    document = _document()
    blocks = _blocks(
        document,
        [
            "1.1 Phạm vi điều chỉnh của văn bản này rất rộng.",
            "1.2 Đối tượng áp dụng gồm nhiều nhóm khác nhau.",
            "1.3 Giải thích từ ngữ được nêu tại đây.",
            "1.4 Nguyên tắc thực hiện phải được tuân thủ.",
        ],
    )

    detection = detect_natural_unit(blocks)

    assert detection.unit is NaturalUnit.CLAUSE
    assert detection.pattern == "dotted"
    assert detection.matches == 4
    assert detection.coverage == 1.0


def test_detection_does_not_depend_on_the_language_of_the_text() -> None:
    """The whole point of dropping the keyword patterns.

    Identical numbering, four different scripts: detection must be identical,
    because the digits carry the structure and the words do not.
    """
    document = _document()
    per_language = {
        "vi": ["1.1 Phạm vi áp dụng.", "1.2 Đối tượng.", "1.3 Nguyên tắc chung."],
        "ja": ["1.1 適用範囲について。", "1.2 対象者について。", "1.3 基本原則について。"],
        "ru": ["1.1 Сфера применения.", "1.2 Круг лиц.", "1.3 Общие принципы."],
        "es": ["1.1 Ámbito de aplicación.", "1.2 Destinatarios.", "1.3 Principios."],
    }

    detections = {
        language: detect_natural_unit(_blocks(document, texts))
        for language, texts in per_language.items()
    }

    assert all(item.unit is NaturalUnit.CLAUSE for item in detections.values())
    assert {item.matches for item in detections.values()} == {3}
    assert {item.pattern for item in detections.values()} == {"dotted"}


def test_clause_sections_are_assigned_from_detected_boundaries() -> None:
    document = _document()
    blocks = _blocks(
        document,
        [
            "1.1 Phạm vi điều chỉnh của văn bản.",
            "Nội dung bổ sung cho mục một.",
            "1.2 Đối tượng áp dụng của văn bản.",
            "1.3 Nguyên tắc chung khi thực hiện.",
        ],
    )

    updated = assign_clause_sections(blocks, detect_natural_unit(blocks))

    assert updated[0].section_path == ["1.1 Phạm vi điều chỉnh của văn bản."]
    assert updated[1].section_path == updated[0].section_path
    assert updated[2].section_path == ["1.2 Đối tượng áp dụng của văn bản."]


def test_coverage_test_rejects_non_monotonic_numbering() -> None:
    document = _document()
    blocks = _blocks(
        document,
        [
            "1.1 Nội dung thứ nhất của tài liệu.",
            "1.7 Nội dung thứ hai của tài liệu.",
            "1.2 Nội dung thứ ba của tài liệu.",
        ],
    )

    assert detect_natural_unit(blocks).unit is NaturalUnit.NONE


def test_coverage_test_rejects_low_coverage_matches() -> None:
    document = _document()
    filler = "Đoạn văn dài không có cấu trúc đánh số nào cả. " * 40
    blocks = _blocks(
        document,
        [
            filler,
            "1.1 Ghi chú ngắn.",
            "1.2 Ghi chú ngắn.",
            "1.3 Ghi chú ngắn.",
        ],
    )

    detection = detect_natural_unit(blocks)

    assert detection.unit is NaturalUnit.NONE


def test_a_plain_numbered_list_is_not_mistaken_for_structure() -> None:
    """`1.` without a sub-number is deliberately not a clause marker.

    Single-level numbering is far more often an ordinary list than a document
    hierarchy, so the pattern requires at least two components.
    """
    document = _document()
    blocks = _blocks(
        document,
        [
            "1. Chuẩn bị hồ sơ theo hướng dẫn.",
            "2. Nộp hồ sơ tại bộ phận một cửa.",
            "3. Nhận kết quả theo giấy hẹn.",
        ],
    )

    assert detect_natural_unit(blocks).unit is NaturalUnit.NONE


def test_existing_section_path_wins_over_clause_detection() -> None:
    document = _document()
    blocks = _blocks(document, ["1.1 A dài hơn.", "1.2 B dài hơn.", "1.3 C dài hơn."])
    blocks[0] = blocks[0].model_copy(update={"section_path": ["Chương I"]})

    detection = detect_natural_unit(blocks)

    assert detection.unit is NaturalUnit.HEADING
    assert assign_clause_sections(blocks, detection) == blocks


def test_heading_blocks_are_detected_without_section_path() -> None:
    document = _document()
    blocks = _blocks(document, ["Introduction", "Body text goes here."])
    blocks[0] = blocks[0].model_copy(update={"type": BlockType.HEADING})

    assert detect_natural_unit(blocks).unit is NaturalUnit.HEADING


def test_empty_document_has_no_natural_unit() -> None:
    assert detect_natural_unit([]).unit is NaturalUnit.NONE
