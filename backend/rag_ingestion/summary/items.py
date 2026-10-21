"""The stored form of the summary: `[sN] text` lines in one text column.

`render_items` is used both for the value written to `threads.summary` and for
the list shown to the model in the fold prompt, so the two can never drift.
No migration: the column stays text, and prose written by the old summarizer
parses as a single item (see `parse_items`).
"""

from __future__ import annotations

import re

from rag_ingestion.config import DEFAULT_SUMMARY_ITEM_MAX_CHARS
from rag_ingestion.schemas.summary import SummaryItem

#: `[s3] text` (written before counters existed) and `[s3|u:4|f:9] text` both
#: parse; the counters default to zero, so no migration and no lost items.
_ITEM_LINE = re.compile(
    r"^\[(s[1-9][0-9]*)(?:\|u:(\d+)\|f:(\d+))?\]\s+(\S.*)$"
)


def parse_items(summary: str | None) -> list[SummaryItem]:
    """Read the stored summary, in stored order.

    Order matters: the list is kept in recency-of-touch order and eviction
    takes from the front, so re-sorting by id here would quietly restore
    the old compress-the-oldest behavior.

    Prose from before this format becomes one `s1` item. That keeps it in
    the prompt and lets the model retire it by name later, which beats both
    dropping it and leaving it unaddressable.
    """
    if not summary or not summary.strip():
        return []
    items: list[SummaryItem] = []
    for line in summary.splitlines():
        match = _ITEM_LINE.match(line.strip())
        if match is None:
            continue
        items.append(
            SummaryItem(
                id=match.group(1),
                used=int(match.group(2) or 0),
                folds=int(match.group(3) or 0),
                text=match.group(4)[:DEFAULT_SUMMARY_ITEM_MAX_CHARS],
            )
        )
    if items:
        return items
    return [
        SummaryItem(
            id="s1",
            text=" ".join(summary.split())[:DEFAULT_SUMMARY_ITEM_MAX_CHARS],
        )
    ]


def render_items(items: list[SummaryItem]) -> str:
    """The stored form, counters included."""
    return "\n".join(
        f"[{item.id}|u:{item.used}|f:{item.folds}] {item.text}" for item in items
    )


def render_for_prompt(items: list[SummaryItem]) -> str:
    """The form a model sees: ids and text, no counters.

    Counters are bookkeeping for eviction. Showing them invites the model to
    reason about the numbers ("this one has a high count, it must matter"),
    which is noise, and it blurs the line the whole design rests on: the model
    proposes content, code owns structure and lifetime.
    """
    return "\n".join(f"[{item.id}] {item.text}" for item in items)
