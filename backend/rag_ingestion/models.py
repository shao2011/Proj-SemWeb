from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    IMAGE = "image"


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    FIGURE = "figure"
    FORMULA = "formula"
    CAPTION = "caption"
    CODE = "code"
    SLIDE = "slide"
    SPEECH_TURN = "speech_turn"


class RelationType(StrEnum):
    CONTINUES = "continues"
    CAPTION_OF = "caption_of"
    REFERS_TO = "refers_to"
    DEFINES = "defines"
    CONTAINS = "contains"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageBBoxLocator(StrictModel):
    kind: Literal["page_bbox"] = "page_bbox"
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]

    @model_validator(mode="after")
    def validate_geometry(self) -> PageBBoxLocator:
        """A box has to be a box.

        `(x0, y0, x1, y1)` with the corners the wrong way round passes every
        field check and still produces an empty or inverted crop when someone
        follows the citation back to the page, which is the one thing provenance
        exists to make possible. A parser whose source uses the other corner
        order normalizes on the way in rather than storing it reversed.
        """
        x0, y0, x1, y1 = self.bbox
        if x1 < x0 or y1 < y0:
            raise ValueError(
                f"bbox corners are reversed: ({x0}, {y0}) to ({x1}, {y1})"
            )
        for name, value in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
            if not math.isfinite(value):
                raise ValueError(f"bbox {name} is not a finite number: {value!r}")
        return self


class CellRangeLocator(StrictModel):
    kind: Literal["cell_range"] = "cell_range"
    sheet: str = Field(min_length=1)
    range: str = Field(min_length=1)

    @field_validator("range")
    @classmethod
    def normalize_range(cls, value: str) -> str:
        return value.upper()


class TimeSpanLocator(StrictModel):
    kind: Literal["time_span"] = "time_span"
    t0: float = Field(ge=0)
    t1: float = Field(ge=0)
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_span(self) -> TimeSpanLocator:
        """A span cannot end before it starts.

        Both bounds were checked against zero and never against each other, so a
        reversed pair passed and playback from the citation would seek to nothing.
        """
        if self.t1 < self.t0:
            raise ValueError(f"time span ends before it starts: {self.t0} to {self.t1}")
        return self


class DomPathLocator(StrictModel):
    kind: Literal["dom_path"] = "dom_path"
    selector: str = Field(min_length=1)
    text_anchor: str = Field(min_length=1)


class OfficePathLocator(StrictModel):
    kind: Literal["office_path"] = "office_path"
    part: str = Field(min_length=1)
    path: str = Field(min_length=1)


Locator = Annotated[
    PageBBoxLocator
    | CellRangeLocator
    | TimeSpanLocator
    | DomPathLocator
    | OfficePathLocator,
    Field(discriminator="kind"),
]


class Relation(StrictModel):
    type: RelationType
    target_block_id: str = Field(min_length=1)


class Document(StrictModel):
    doc_id: str
    owner_user_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source_type: SourceType
    source_uri: str
    content_hash: str
    title: str | None = None
    languages: list[str] = Field(default_factory=list)
    doc_type: str | None = None
    domain: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_date: datetime | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    parse_engine: str
    parse_version: str
    overall_confidence: float = Field(default=1.0, ge=0, le=1)

    @classmethod
    def from_content(
        cls,
        *,
        content: bytes,
        owner_user_id: str,
        project_id: str,
        source_type: SourceType,
        source_uri: str,
        parse_engine: str,
        parse_version: str,
        **metadata: object,
    ) -> Document:
        content_hash = hashlib.sha256(content).hexdigest()
        identity = "\x1f".join(
            (owner_user_id, project_id, source_type.value, source_uri, content_hash)
        )
        doc_id = f"doc_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return cls(
            doc_id=doc_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            source_type=source_type,
            source_uri=source_uri,
            content_hash=content_hash,
            parse_engine=parse_engine,
            parse_version=parse_version,
            **metadata,
        )


class Block(StrictModel):
    block_id: str
    doc_id: str
    order: int = Field(ge=0)
    type: BlockType
    text: str
    confidence: float = Field(ge=0, le=1)
    low_confidence: bool = False
    language: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    parent_block_id: str | None = None
    section_path: list[str] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    media_ref: str | None = None
    locators: list[Locator] = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)

    @classmethod
    def from_source(
        cls,
        *,
        doc_id: str,
        order: int,
        type: BlockType,
        text: str,
        confidence: float,
        locators: list[Locator],
        **metadata: object,
    ) -> Block:
        locator_payload = [locator.model_dump(mode="json") for locator in locators]
        identity = json.dumps(
            [doc_id, order, type.value, text, locator_payload],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        block_id = f"blk_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return cls(
            block_id=block_id,
            doc_id=doc_id,
            order=order,
            type=type,
            text=text,
            confidence=confidence,
            locators=locators,
            **metadata,
        )


class ParseResult(StrictModel):
    document: Document
    blocks: list[Block]

