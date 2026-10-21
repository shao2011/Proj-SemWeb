"""A deck read by rendering it, rather than by reading its shape tree.

The native reader cannot see a picture, and most of what a deck means lives in
its pictures -- a diagram drawn as shapes has no text saying which box points
at which, and a diagram pasted in as one flat image has no text at all.
"""

from __future__ import annotations

import base64
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from rag_ingestion.ingestion.parsers.datalab import ConvertedBlock, content_type_for
from rag_ingestion.ingestion.parsers.office import parse_pptx
from rag_ingestion.models import BlockType, OfficePathLocator, SourceType
from rag_ingestion.storage.media import LocalMediaStore

PNG = b"\x89PNG\r\n\x1a\n" + b"rendered-diagram" * 4


class FakeConverter:
    """Stands in for Datalab, returning what a real conversion returns."""

    def __init__(self, blocks: list[ConvertedBlock]) -> None:
        self.blocks = blocks
        self.calls: list[Path] = []

    def convert(self, path: Path) -> list[ConvertedBlock]:
        self.calls.append(path)
        return self.blocks


class BrokenConverter:
    def convert(self, path: Path) -> list[ConvertedBlock]:
        raise RuntimeError("Datalab is down")


def _block(
    text: str,
    page: int,
    block_type: BlockType,
    *,
    media: bytes | None = None,
    media_name: str | None = None,
    furniture: bool = False,
) -> ConvertedBlock:
    return ConvertedBlock(
        text=text,
        page=page,
        bbox=(0.0, 0.0, 10.0, 10.0),
        block_type=block_type,
        media=media,
        media_name=media_name,
        furniture=furniture,
    )


def _deck(tmp_path: Path) -> Path:
    """A minimal readable deck; its content does not matter to these tests."""
    path = tmp_path / "briefing.pptx"
    presentation = (
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slide" Target="slides/slide1.xml"/></Relationships>'
    )
    slide = (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody>'
        "<a:p><a:r><a:t>Native title</a:t></a:r></a:p></p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", rels)
        archive.writestr("ppt/slides/slide1.xml", slide)
    return path


def _parse(path: Path, **extra):
    return parse_pptx(
        path,
        owner_user_id="user-a",
        project_id="project-a",
        source_uri=f"upload://{path.name}",
        **extra,
    )


def test_a_pptx_is_offered_to_the_service_as_a_powerpoint(tmp_path: Path) -> None:
    """It refused the upload outright while the type said octet-stream:

    `Invalid file type. Only PDF files, word documents, spreadsheets,
    powerpoints, HTML, and PNG, JPG, GIF, TIFF, and WEBP images are accepted.`
    """
    assert content_type_for(Path("deck.pptx")) == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_a_converted_deck_becomes_one_section_per_slide(tmp_path: Path) -> None:
    converter = FakeConverter(
        [
            _block("Overview", 1, BlockType.HEADING),
            _block("Receipt arrives by fax", 1, BlockType.PARAGRAPH),
            _block("Architecture", 2, BlockType.HEADING),
            _block("Three services", 2, BlockType.PARAGRAPH),
        ]
    )

    result = _parse(_deck(tmp_path), converter=converter)

    assert result.document.source_type is SourceType.PPTX
    slides = [block for block in result.blocks if block.type is BlockType.SLIDE]
    assert [block.text for block in slides] == ["Overview", "Architecture"]
    assert {tuple(block.section_path) for block in result.blocks} == {
        ("Overview",),
        ("Architecture",),
    }


def test_a_figure_keeps_its_description_and_its_rendered_picture(
    tmp_path: Path,
) -> None:
    """The whole reason for converting: a diagram drawn as shapes has no image
    in the file at all, and the description is the only searchable text it has.
    """
    description = "The diagram shows incomingA feeding receiveDocument via SSM."
    converter = FakeConverter(
        [
            _block("AWS Architecture", 1, BlockType.HEADING),
            # The name says jpg and the bytes say png. The bytes win, the same
            # way they do for a picture pulled out of a workbook.
            _block(description, 1, BlockType.FIGURE, media=PNG, media_name="d_img.jpg"),
        ]
    )
    media = LocalMediaStore(tmp_path / "media")

    result = _parse(_deck(tmp_path), converter=converter, media_store=media)

    figure = next(block for block in result.blocks if block.type is BlockType.FIGURE)
    assert figure.text == description
    assert media.get(figure.media_ref) == PNG
    assert figure.media_ref.endswith(".png")


def test_a_figure_without_a_media_store_keeps_its_description(tmp_path: Path) -> None:
    converter = FakeConverter(
        [
            _block("AWS Architecture", 1, BlockType.HEADING),
            _block("A diagram of three services.", 1, BlockType.FIGURE, media=PNG),
        ]
    )

    result = _parse(_deck(tmp_path), converter=converter)

    figure = next(block for block in result.blocks if block.type is BlockType.FIGURE)
    assert figure.media_ref is None
    assert figure.text == "A diagram of three services."


def test_a_picture_with_nothing_to_say_is_dropped(tmp_path: Path) -> None:
    """A logo on the master slide comes back on every page with no description,
    and eighteen copies of it are eighteen chunks of nothing."""
    converter = FakeConverter(
        [
            _block("Overview", 1, BlockType.HEADING),
            _block("", 1, BlockType.FIGURE, media=PNG, media_name="logo.jpg"),
            _block("Body text", 1, BlockType.PARAGRAPH),
        ]
    )
    media = LocalMediaStore(tmp_path / "media")

    result = _parse(_deck(tmp_path), converter=converter, media_store=media)

    assert not [block for block in result.blocks if block.type is BlockType.FIGURE]


def test_the_logo_on_every_slide_is_dropped(tmp_path: Path) -> None:
    """A master-slide logo is captioned, so having a caption is not enough to
    make a picture content. Eighteen copies of `Suzuyo Group logo` were a
    quarter of the chunks the deck produced."""
    converter = FakeConverter(
        [
            block
            for slide in range(1, 6)
            for block in (
                _block(f"Slide {slide} title", slide, BlockType.HEADING),
                _block("Suzuyo Group logo", slide, BlockType.FIGURE, media=PNG),
                _block(f"Diagram for slide {slide}", slide, BlockType.FIGURE, media=PNG),
            )
        ]
    )
    media = LocalMediaStore(tmp_path / "media")

    result = _parse(_deck(tmp_path), converter=converter, media_store=media)

    figures = [block.text for block in result.blocks if block.type is BlockType.FIGURE]
    assert "Suzuyo Group logo" not in figures
    assert len(figures) == 5


def test_a_picture_that_repeats_only_twice_is_kept(tmp_path: Path) -> None:
    """Two slides can legitimately show the same diagram; eighteen cannot."""
    converter = FakeConverter(
        [
            block
            for slide in (1, 2)
            for block in (
                _block(f"Slide {slide}", slide, BlockType.HEADING),
                _block("The same architecture diagram", slide, BlockType.FIGURE, media=PNG),
            )
        ]
    )

    result = _parse(_deck(tmp_path), converter=converter)

    figures = [block.text for block in result.blocks if block.type is BlockType.FIGURE]
    assert figures == ["The same architecture diagram"] * 2


def test_running_headers_and_footers_are_dropped(tmp_path: Path) -> None:
    """`3 / 17` on every slide is furniture, not content."""
    converter = FakeConverter(
        [
            _block("Overview", 1, BlockType.HEADING),
            _block("3 / 17", 1, BlockType.PARAGRAPH, furniture=True),
            _block("Real content", 1, BlockType.PARAGRAPH),
        ]
    )

    result = _parse(_deck(tmp_path), converter=converter)

    assert [block.text for block in result.blocks] == ["Overview", "Real content"]


def test_the_native_reader_takes_over_when_the_service_fails(tmp_path: Path) -> None:
    """A provider outage must not stop an upload.

    The native reader is worse -- it cannot see a picture -- but it is offline,
    free, and always available, so it is the floor rather than the ceiling.
    """
    result = _parse(_deck(tmp_path), converter=BrokenConverter())

    assert [block.text for block in result.blocks] == ["Native title"]
    assert result.blocks[0].locators == [
        OfficePathLocator(part="ppt/slides/slide1.xml", path="/sld/cSld")
    ]


def test_the_native_reader_takes_over_when_the_service_reads_nothing(
    tmp_path: Path,
) -> None:
    result = _parse(_deck(tmp_path), converter=FakeConverter([]))

    assert [block.text for block in result.blocks] == ["Native title"]


def test_a_slide_with_no_heading_is_still_addressable(tmp_path: Path) -> None:
    converter = FakeConverter([_block("Just a sentence.", 4, BlockType.PARAGRAPH)])

    result = _parse(_deck(tmp_path), converter=converter)

    assert result.blocks[0].type is BlockType.SLIDE
    assert result.blocks[0].text == "Slide 4"
    assert result.blocks[0].section_path == ["Slide 4"]


def test_converted_blocks_carry_the_slide_they_came_from(tmp_path: Path) -> None:
    converter = FakeConverter(
        [
            _block("Overview", 1, BlockType.HEADING),
            _block("Body", 1, BlockType.PARAGRAPH),
            _block("Later", 7, BlockType.PARAGRAPH),
        ]
    )

    result = _parse(_deck(tmp_path), converter=converter)

    assert {block.metadata["slide"] for block in result.blocks} == {1, 7}
    body = next(block for block in result.blocks if block.text == "Body")
    assert body.locators[0].part == "ppt/slides/slide1.xml"


@pytest.mark.parametrize("page", [0, -3])
def test_a_block_on_an_impossible_page_is_refused(tmp_path: Path, page: int) -> None:
    """Pages are one-based here and zero-based in the response; getting that
    wrong silently shifts every citation by one slide."""
    converter = FakeConverter([_block("Overview", page, BlockType.HEADING)])

    with pytest.raises(ValueError, match="slide number"):
        _parse(_deck(tmp_path), converter=converter)
