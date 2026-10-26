"""Two policy questions about a memory, answered without a database.

Both are pure functions of their arguments, so the policy is testable without
a store or a provider.

Scope is not one of them: it is fixed by which tool the model called, so
there is nothing to infer.
"""

from __future__ import annotations

#: The same key written in several different threads is the evidence that a
#: situational preference is really a habit, so the promotion runs
#: automatically. Five rather than two: one repeat is coincidence, and a wrong
#: promotion leaves a global memory the user never asked for.
PROMOTION_THREAD_THRESHOLD = 5


def normalize(value: str) -> str:
    """Compare values the way a reader would, so a restatement is not a change."""
    return " ".join(value.split()).casefold()


def should_promote(
    *, distinct_threads: int, threshold: int = PROMOTION_THREAD_THRESHOLD
) -> bool:
    return distinct_threads >= threshold
