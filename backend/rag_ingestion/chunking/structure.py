from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from rag_ingestion.models import Block, BlockType

MIN_UNIT_MATCHES = 3
MIN_COVERAGE = 0.6
MAX_SINGLE_UNIT_SHARE = 0.9


class NaturalUnit(StrEnum):
    HEADING = "heading"
    CLAUSE = "clause"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ClausePattern:
    name: str
    pattern: re.Pattern[str]


#: Numeric enumeration only. Keyword patterns (`Điều`, `Article`, `Section`,
#: `§`) used to live here and were removed: a keyword list covers a handful of
#: languages and one domain, so it silently does nothing on the majority of a
#: general corpus while implying it works. Digits carry the structure in every
#: writing system, and `_passes_coverage_test` rejects a match that does not
#: actually explain the document.
CLAUSE_PATTERNS: tuple[ClausePattern, ...] = (
    ClausePattern("dotted", re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s+\S")),
)


@dataclass(frozen=True, slots=True)
class StructureDetection:
    unit: NaturalUnit
    pattern: str | None = None
    matches: int = 0
    coverage: float = 0.0
    starts: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.unit is not NaturalUnit.NONE


def _ordinal(label: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", label))


def _passes_coverage_test(
    starts: list[int], labels: list[str], sizes: list[int], total_chars: int
) -> tuple[bool, float]:
    """Reject a candidate unless it looks like real document structure."""
    if len(starts) < MIN_UNIT_MATCHES:
        return False, 0.0

    ordinals = [_ordinal(label) for label in labels]
    if any(
        current <= previous
        for previous, current in zip(ordinals, ordinals[1:], strict=False)
    ):
        return False, 0.0

    covered = sum(sizes)
    coverage = covered / total_chars if total_chars else 0.0
    if coverage < MIN_COVERAGE:
        return False, coverage
    if covered and max(sizes) / covered > MAX_SINGLE_UNIT_SHARE:
        return False, coverage
    return True, coverage


def detect_natural_unit(blocks: list[Block]) -> StructureDetection:
    """Detect the addressable unit of a document from its own text.

    Structure is a property of the document, not of the domain, so this runs
    without any domain configuration and reports when it fails.
    """
    ordered = sorted(blocks, key=lambda block: block.order)
    if not ordered:
        return StructureDetection(unit=NaturalUnit.NONE)

    if any(block.section_path for block in ordered):
        return StructureDetection(
            unit=NaturalUnit.HEADING,
            pattern="section_path",
            matches=sum(1 for block in ordered if block.section_path),
            coverage=1.0,
        )
    if any(block.type is BlockType.HEADING and block.text.strip() for block in ordered):
        return StructureDetection(
            unit=NaturalUnit.HEADING,
            pattern="heading_block",
            matches=sum(1 for block in ordered if block.type is BlockType.HEADING),
            coverage=1.0,
        )

    total_chars = sum(len(block.text) for block in ordered)
    for candidate in CLAUSE_PATTERNS:
        starts: list[int] = []
        labels: list[str] = []
        for index, block in enumerate(ordered):
            match = candidate.pattern.match(block.text)
            if match:
                starts.append(index)
                labels.append(match.group(1))
        if not starts:
            continue

        boundaries = [*starts, len(ordered)]
        sizes = [
            sum(len(ordered[position].text) for position in range(begin, end))
            for begin, end in zip(boundaries, boundaries[1:], strict=False)
        ]
        passed, coverage = _passes_coverage_test(starts, labels, sizes, total_chars)
        if passed:
            return StructureDetection(
                unit=NaturalUnit.CLAUSE,
                pattern=candidate.name,
                matches=len(starts),
                coverage=coverage,
                starts=starts,
                labels=labels,
            )

    return StructureDetection(unit=NaturalUnit.NONE)


def assign_clause_sections(
    blocks: list[Block], detection: StructureDetection
) -> list[Block]:
    """Populate ``section_path`` from detected clause boundaries.

    Returns the blocks unchanged when no clause structure was detected, so the
    caller can always route through this function.
    """
    if detection.unit is not NaturalUnit.CLAUSE or not detection.starts:
        return blocks

    ordered = sorted(blocks, key=lambda block: block.order)
    label_by_index: dict[int, str] = {}
    boundaries = [*detection.starts, len(ordered)]
    for (begin, end), label in zip(
        zip(boundaries, boundaries[1:], strict=False), detection.labels, strict=False
    ):
        heading = ordered[begin].text.strip().splitlines()[0][:120]
        for position in range(begin, end):
            label_by_index[position] = heading or label

    updated: list[Block] = []
    for index, block in enumerate(ordered):
        heading = label_by_index.get(index)
        if heading is None or block.section_path:
            updated.append(block)
            continue
        updated.append(block.model_copy(update={"section_path": [heading]}))
    return updated
