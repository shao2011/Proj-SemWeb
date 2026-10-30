"""Which thread messages are replayed verbatim, and when the rest is folded.

Lives outside `rag_ingestion.api` because importing that package builds the
whole runtime app (Postgres, auth). The evaluation harness needs the same
window rules without a web application.
"""

from __future__ import annotations

from rag_ingestion.chat import ChatTurn
from rag_ingestion.storage.threads import ThreadMessage, ThreadMessageRole

#: Conservative default before any measured turn. Vietnamese runs ~2.5
#: chars/token -- lower than English's ~4 -- so the default over-estimates
#: token counts, which errs toward folding early rather than overflowing.
DEFAULT_CHARS_PER_TOKEN = 2.5

#: After a forced fold, the window refills only to this share of its caps.
#: The freed headroom absorbs the next several turns; without it a thread
#: past its cap would pay a summarizer call on every single message.
HISTORY_REFILL_RATIO = 0.7


def as_turn(message: ThreadMessage) -> ChatTurn:
    return ChatTurn(
        role="user" if message.role is ThreadMessageRole.USER else "assistant",
        content=message.content,
    )


def plan_history(
    messages: list[ThreadMessage],
    *,
    floor: int,
    cap: int,
    budget_chars: int,
) -> tuple[list[ThreadMessage], int]:
    """Pick the verbatim window: the opener plus the newest suffix that fits.

    Two ceilings and one floor. The suffix stops at `cap` messages or when
    `budget_chars` is spent, whichever bites first -- but never below `floor`
    messages, because a reply needs its immediate context even when the
    budget disagrees. The opening turn always rides along: it is where the
    subject of the thread is established, and losing it makes every later
    pronoun ambiguous.

    Returns `(kept, boundary)` where `boundary` is the highest position that
    fell out of the window -- 0 when nothing did -- which is exactly how far
    the summary must reach for the prompt to carry the whole conversation.
    """
    if not messages:
        return [], 0
    opener, middle = messages[0], messages[1:]
    if not middle:
        return [opener], 0
    selected: list[ThreadMessage] = []
    spent = len(opener.content)
    for message in reversed(middle):
        over_count = len(selected) >= cap
        over_chars = spent + len(message.content) > budget_chars
        if len(selected) >= floor and (over_count or over_chars):
            break
        selected.append(message)
        spent += len(message.content)
    selected.reverse()
    boundary = selected[0].position - 1 if selected else middle[-1].position
    # The opener itself never counts as fallen out.
    return [opener, *selected], max(0, boundary if boundary > opener.position else 0)
