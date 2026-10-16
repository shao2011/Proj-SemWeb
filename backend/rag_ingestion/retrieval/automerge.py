from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag_ingestion.chunking import Chunk, ChunkLevel


def token_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


@dataclass(slots=True)
class MergedContext:
    chunks: list[Chunk] = field(default_factory=list)
    evidence_leaf_ids: list[str] = field(default_factory=list)
    merge_count: int = 0
    used_tokens: int = 0


def descendant_leaf_ids(chunk: Chunk, chunks_by_id: dict[str, Chunk]) -> list[str]:
    """Resolve any chunk down to the citable leaves it covers, in document order."""
    if chunk.level is ChunkLevel.LEAF:
        return [chunk.chunk_id]

    leaves: list[str] = []
    visited: set[str] = set()

    def walk(current: Chunk) -> None:
        for child_id in current.child_ids:
            if child_id in visited:
                continue
            visited.add(child_id)
            child = chunks_by_id.get(child_id)
            if child is None:
                continue
            if child.level is ChunkLevel.LEAF:
                leaves.append(child.chunk_id)
            else:
                walk(child)

    walk(chunk)
    return list(dict.fromkeys(leaves))


def _covers(
    candidate: Chunk, other: Chunk, chunks_by_id: dict[str, Chunk]
) -> bool:
    if candidate.chunk_id == other.chunk_id:
        return True
    return other.chunk_id in set(descendant_leaf_ids(candidate, chunks_by_id))


def auto_merge(
    ranked: list[Chunk],
    chunks_by_id: dict[str, Chunk],
    *,
    token_budget: int,
) -> MergedContext:
    """Promote sibling leaves to their parent when the parent earns its tokens.

    Implements the three HiChunk Auto-Merge conditions: enough of the parent is
    already covered, at least two siblings were recalled, and the extra parent
    tokens still fit the budget. Climbs while all three hold.
    """
    if token_budget <= 0:
        return MergedContext()

    selected: list[Chunk] = []
    merge_count = 0

    def used_tokens() -> int:
        return sum(token_count(chunk.source_text) for chunk in selected)

    for candidate in ranked:
        if any(_covers(chosen, candidate, chunks_by_id) for chosen in selected):
            continue

        current = candidate
        parent = chunks_by_id.get(current.parent_id or "")
        if parent is not None and len(parent.child_ids) == 1:
            current = parent
            parent = chunks_by_id.get(current.parent_id or "")

        selected.append(current)

        while parent is not None:
            siblings = [
                chosen
                for chosen in selected
                if chosen.parent_id == parent.chunk_id
            ]
            parent_chars = len(parent.source_text)
            child_chars = sum(len(chosen.source_text) for chosen in siblings)
            spent = used_tokens()
            threshold = (parent_chars / 3) * (1 + spent / token_budget)

            covered_tokens = sum(token_count(chosen.source_text) for chosen in siblings)
            parent_tokens = token_count(parent.source_text)
            remaining = token_budget - spent

            if not (
                child_chars >= threshold
                and len(siblings) >= 2
                and (parent_tokens - covered_tokens) <= remaining
            ):
                break

            selected = [
                chosen for chosen in selected if chosen.parent_id != parent.chunk_id
            ]
            selected.append(parent)
            merge_count += 1
            parent = chunks_by_id.get(parent.parent_id or "")

        if used_tokens() >= token_budget:
            break

    evidence: list[str] = []
    for chunk in selected:
        evidence.extend(descendant_leaf_ids(chunk, chunks_by_id))

    return MergedContext(
        chunks=selected,
        evidence_leaf_ids=list(dict.fromkeys(evidence)),
        merge_count=merge_count,
        used_tokens=used_tokens(),
    )
