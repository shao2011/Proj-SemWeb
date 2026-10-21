"""Apply the model's delta to the item list.

This is the step ACE (ICLR 2026) insists must be "lightweight, non-LLM
logic": the model proposes, deterministic code disposes. An item the delta
never names does not pass through a model here or anywhere else, which is
the retention guarantee the old rewrite-the-whole-summary loop could not
make.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from rag_ingestion.config import (
    DEFAULT_SUMMARY_ITEM_MAX_CHARS,
    DEFAULT_SUMMARY_MAX_OPS,
)
from rag_ingestion.logger import get_logger
from rag_ingestion.memory.routing import normalize
from rag_ingestion.schemas.summary import SummaryItem, SummaryOp, SummaryOpKind

_log = get_logger(__name__)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped.removeprefix("```json").removeprefix("```")
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def parse_facts(raw_text: str, *, max_facts: int = DEFAULT_SUMMARY_MAX_OPS) -> list[str]:
    """The extract phase's reply as a list of distilled facts.

    Raises `ValueError` when the reply is not JSON at all, which is the
    caller's cue to abandon the fold and keep the previous summary rather
    than curate against garbage.
    """
    payload = json.loads(_strip_code_fence(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("Extracted facts must be a JSON object")
    raw_facts = payload.get("facts") or []
    if not isinstance(raw_facts, list):
        raise ValueError("facts must be a list or null")
    facts = [
        " ".join(str(item).split())[:DEFAULT_SUMMARY_ITEM_MAX_CHARS]
        for item in raw_facts
        if isinstance(item, str) and item.strip()
    ]
    if len(facts) > max_facts:
        _log.info(
            "Extract returned more than %d facts; the rest are dropped", max_facts
        )
    return facts[:max_facts]


def parse_delta(raw_text: str, *, max_ops: int = DEFAULT_SUMMARY_MAX_OPS) -> list[SummaryOp]:
    """The model's reply as validated operations.

    Raises `ValueError` when the reply is not JSON at all, which is the
    caller's cue to keep the previous summary. A malformed operation inside
    an otherwise good reply is only skipped: one bad op must not cost the
    fold its good ones.
    """
    payload = json.loads(_strip_code_fence(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("Summary delta must be a JSON object")
    raw_ops = payload.get("ops") or []
    if not isinstance(raw_ops, list):
        raise ValueError("ops must be a list or null")

    ops: list[SummaryOp] = []
    for item in raw_ops:
        if len(ops) >= max_ops:
            _log.info(
                "Summary delta asked for more than %d ops; the rest are dropped",
                max_ops,
            )
            break
        try:
            ops.append(SummaryOp.model_validate(item))
        except ValidationError:
            continue
    return ops


def apply_ops(
    items: list[SummaryItem],
    ops: list[SummaryOp],
    *,
    max_items: int,
) -> tuple[list[SummaryItem], list[SummaryItem]]:
    """Apply the delta. Returns the new list and the items eviction took.

    Evicted items come back whole rather than as ids: they are gone from the
    stored column by the time anyone reads the log line, so an id on its own
    names a row that can no longer be looked up.

    List order is recency of touch, not creation: an UPDATE moves its item
    to the back, and eviction takes from the front. Evicting by id order
    instead would re-create the old prompt's rule -- compress the oldest
    first -- which is exactly how a fact stated on turn 3 used to disappear
    while a stale one from turn 40 survived.
    """
    result = list(items)
    before = {item.id for item in items}
    next_ordinal = max((int(item.id[1:]) for item in result), default=0) + 1

    for op in ops:
        if op.op is SummaryOpKind.ADD:
            text = (op.text or "").strip()[:DEFAULT_SUMMARY_ITEM_MAX_CHARS]
            # Mechanical duplicate guard. The curator is asked to UPDATE a
            # fact it already holds, but an UPDATE naming an id that does not
            # exist is skipped by design -- so the next fold sees the fact
            # missing and ADDs it again. That loop mints duplicates no model
            # quality can fix, so a restatement is folded into a touch here.
            # Exact match after normalization only: a semantic check would
            # merge "deadline for A is 30/9" with "deadline for B is 30/9",
            # and losing a real fact is worse than carrying a near-duplicate.
            twin = next(
                (
                    position
                    for position, item in enumerate(result)
                    if normalize(item.text) == normalize(text)
                ),
                None,
            )
            if twin is not None:
                _log.info(
                    "Summary ADD restates %s; folded into a touch",
                    result[twin].id,
                )
                result.append(result.pop(twin))
                continue
            # Counters start at zero, and `folds` stays zero through this
            # fold's ageing pass below, which is what keeps a freshly minted
            # item out of the eviction pool: it has had no chance to be used.
            result.append(SummaryItem(id=f"s{next_ordinal}", text=text))
            next_ordinal += 1
            continue
        index = next(
            (position for position, item in enumerate(result) if item.id == op.id),
            None,
        )
        if index is None:
            # Named an item that is not there. Skipped, not fatal: the rest
            # of the delta is still good, and a silent skip is safer than
            # letting a hallucinated id remove a real item.
            _log.info(
                "Summary op %s references unknown item %s; skipped",
                op.op.value,
                op.id,
            )
            continue
        removed = result.pop(index)
        if op.op is SummaryOpKind.UPDATE:
            # Counters carry over: a correction is the same fact restated,
            # not a new one, so the standing it earned survives the edit.
            result.append(
                removed.model_copy(
                    update={
                        "text": (op.text or "").strip()[
                            :DEFAULT_SUMMARY_ITEM_MAX_CHARS
                        ]
                    }
                )
            )

    # Age everything that was already here. Items minted by this fold stay at
    # zero, which is what keeps them out of the eviction pool below: their
    # zero score means "no chance yet", not "failed to earn its place".
    result = [item.older() if item.id in before else item for item in result]

    evicted: list[SummaryItem] = []
    while len(result) > max_items:
        victim = _weakest(result)
        if victim is None:
            # Unreachable while one fold emits fewer ops than the cap, so
            # every overflow has at least one aged item. Logged rather than
            # asserted: raising the op cap later must not create a crash.
            _log.warning("Summary over cap with no evictable item; taking the front")
            victim = 0
        evicted.append(result.pop(victim))
    return result, evicted


def _weakest(items: list[SummaryItem]) -> int | None:
    """Index of the item to evict, or None when every item is too new.

    Ranked on `used / (folds + 1)` -- usefulness per chance to be useful.
    Only items that have survived a fold are eligible: a brand-new item has
    a zero score because nothing has happened yet, not because it failed.

    Ties (most often several items at zero) break toward the one that has had
    the most chances, then toward the one the model has left untouched
    longest -- which is the position it holds in the list.
    """
    ranked = [
        (item.used / (item.folds + 1), -item.folds, position)
        for position, item in enumerate(items)
        if item.folds >= 1
    ]
    if not ranked:
        return None
    return min(ranked)[2]
