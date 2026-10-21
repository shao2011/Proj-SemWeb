"""Two policy questions about a memory, answered without a database.

Both are pure functions of their arguments, so the policy is testable without
a store or a provider.

Scope routing used to live here too -- a `key`-to-scope table, because the
extractor's own `suggested_scope` flipped between runs of the same sentence.
It is gone: scope is now fixed by which tool the model called, so there is
nothing left to infer.
"""

from __future__ import annotations

#: Section 6.5 of the design: the same key written in several different threads
#: is the evidence that a situational preference is really a habit. The design
#: proposes the promotion rather than applying it; this runs it automatically,
#: which is a deliberate deviation recorded in IMPLEMENT.md. Five rather than
#: the design's two: one repeat is coincidence, and a wrong promotion leaves a
#: global memory the user never asked for.
PROMOTION_THREAD_THRESHOLD = 5


def normalize(value: str) -> str:
    """Compare values the way a reader would, so a restatement is not a change."""
    return " ".join(value.split()).casefold()


def should_promote(
    *, distinct_threads: int, threshold: int = PROMOTION_THREAD_THRESHOLD
) -> bool:
    return distinct_threads >= threshold
