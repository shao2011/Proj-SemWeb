from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.config import DEFAULT_MEMORY_CHAR_BUDGET, DEFAULT_MEMORY_MAX_ITEMS
from rag_ingestion.prompts.answering import (
    ABOUT_USER_SECTION,
    EVIDENCE_BLOCK,
    MEMORY_OVERFLOW_NOTE,
    NO_EVIDENCE_RESPONSE,
    PREFERENCES_SECTION,
    SUMMARY_SECTION,
    SYSTEM_PROMPT,
    USED_MARKER_INSTRUCTION,
)
from rag_ingestion.schemas.answering import Answer, AnswerEvidence
from rag_ingestion.memory.models import Memory, MemoryScope, MemoryType


#: Combined budget for the memory sections. Memory is injected on every turn, so
#: this is a permanent cost and the overflow is reported rather than dropped.
#: Kept under its historic name for existing importers; the live value now
#: travels in as an argument from `ConversationConfig`.
MEMORY_CHAR_BUDGET = DEFAULT_MEMORY_CHAR_BUDGET

#: Most specific scope wins for a given key.
_SCOPE_RANK = {
    MemoryScope.THREAD: 0,
    MemoryScope.PROJECT: 1,
    MemoryScope.USER: 2,
}

_OVERRIDE_NOTE = {
    MemoryScope.THREAD: "this conversation only",
    MemoryScope.PROJECT: "this project only",
}


def _memory_age(created_at: datetime, *, now: datetime) -> str:
    """Age as a phrase, not a timestamp.

    Models are poor at date arithmetic: an ISO timestamp does not trigger the
    same doubt that "47 days ago" does. Nothing is said for anything a day old
    or newer, where the warning would only be noise.
    """
    days = (now - created_at).days
    if days <= 1:
        return ""
    return f" (saved {days} days ago)"


def _resolve(memories: list[Memory]) -> list[tuple[Memory, bool]]:
    """Keep one memory per key, the most specific scope winning.

    A thread-scoped instruction overrides the user-scoped one for its own
    thread, so both arrive here and only the narrower survives. The flag says
    whether it actually displaced something, which is what earns the override
    note: dropping the loser silently leaves the model free to keep quoting a
    preference the user has just overridden.
    """
    best: dict[str, Memory] = {}
    displaced: set[str] = set()
    for memory in memories:
        current = best.get(memory.key)
        if current is None:
            best[memory.key] = memory
            continue
        displaced.add(memory.key)
        if _SCOPE_RANK[memory.scope] < _SCOPE_RANK[current.scope]:
            best[memory.key] = memory
    return [
        (memory, memory.key in displaced)
        for memory in sorted(best.values(), key=lambda item: item.key)
    ]


def _memory_line(memory: Memory, overrode: bool, *, now: datetime) -> str:
    note = _OVERRIDE_NOTE.get(memory.scope, "") if overrode else ""
    suffix = f" ({note})" if note else ""
    return f"- {memory.key}: {memory.value}{suffix}{_memory_age(memory.created_at, now=now)}"


def last_relevant_at(memory: Memory) -> datetime:
    """When this memory last mattered: read into a prompt, or failing that,
    saved.

    One timeline rather than a "never used" bucket. Creation is the first
    time a memory mattered, so something saved a moment ago ranks with the
    fresh material instead of with the forgotten -- which is also why
    eviction reads the same value. Storage imports this so the memory that
    is carried and the memory that is kept are ranked by one rule.
    """
    return memory.last_used_at or memory.created_at


def _carry_rank(memory: Memory) -> tuple:
    """Which memories earn a place when not all of them fit.

    Ordering used to be alphabetical by key, which starved whatever sorted
    last: the scopes together hold more memory than the prompt can carry, so
    the tail never reached a model, never had `last_used_at` set, and was
    therefore first in line for eviction. Keys beginning with "z" were being
    deleted for being called "z".

    Two signals instead: the narrower scope, which is the one about the
    situation at hand, then recency of relevance. Neither is semantic --
    scoring every memory against the question is a different feature -- but
    both are at least about the memory rather than its name.
    """
    return (
        _SCOPE_RANK[memory.scope],
        -last_relevant_at(memory).timestamp(),
    )


def select_memories(
    memories: list[Memory],
    *,
    now: datetime,
    max_items: int = DEFAULT_MEMORY_MAX_ITEMS,
    char_ceiling: int = DEFAULT_MEMORY_CHAR_BUDGET,
) -> list[Memory]:
    """The memories that actually reach the model.

    Three filters stand between recall and the prompt: a narrower scope
    displaces a wider one on the same key, `max_items` caps how many are
    carried, and `char_ceiling` is a backstop for the case a few very long
    values would otherwise flood the system message. Lines are kept whole or
    left out; half a memory would state something the user never said.

    What survives is what the model reads, which is the only honest
    definition of a memory having been "used" -- and therefore the input to
    `MemoryStore.touch`, and through it to eviction.

    Pure, so the route can recompute exactly what the renderer produced
    without threading a second return value out through `build_turns`.
    """
    resolved = _resolve(memories)
    ranked = sorted(resolved, key=lambda pair: _carry_rank(pair[0]))
    selected: list[Memory] = []
    spent = 0
    for memory, overrode in ranked:
        if len(selected) >= max_items:
            break
        line = _memory_line(memory, overrode, now=now)
        if spent + len(line) > char_ceiling:
            continue
        spent += len(line)
        selected.append(memory)
    return selected


def _memory_sections(
    memories: list[Memory],
    *,
    now: datetime,
    max_items: int,
    char_ceiling: int,
) -> list[str]:
    """Render the fact and preference sections from the carried memories.

    Selection is ranked; rendering is alphabetical. A system message whose
    lines reshuffle between turns is a prompt prefix no provider can cache,
    so what changes here is *which* lines appear, never their order.
    """
    carried = select_memories(
        memories, now=now, max_items=max_items, char_ceiling=char_ceiling
    )
    resolved = _resolve(memories)
    displaced = {memory.key for memory, overrode in resolved if overrode}
    dropped = len(resolved) - len(carried)
    facts: list[str] = []
    preferences: list[str] = []
    for memory in sorted(carried, key=lambda item: item.key):
        line = _memory_line(memory, memory.key in displaced, now=now)
        if memory.type is MemoryType.FACT:
            facts.append(line)
        else:
            preferences.append(line)

    sections: list[str] = []
    if facts:
        sections.append(ABOUT_USER_SECTION.format(facts="\n".join(facts)))
    if preferences:
        sections.append(
            PREFERENCES_SECTION.format(preferences="\n".join(preferences))
        )
    if dropped:
        # Named, not silently cut: a user who cannot see that something was
        # dropped concludes the model is ignoring them.
        sections.append(
            MEMORY_OVERFLOW_NOTE.format(dropped=dropped, budget=max_items)
        )
    return sections


def build_turns(
    question: str,
    evidence: list["AnswerEvidence"],
    *,
    history: list[ChatTurn] | None = None,
    memory: list[Memory] | None = None,
    summary: str | None = None,
    now: datetime | None = None,
    memory_max_items: int = DEFAULT_MEMORY_MAX_ITEMS,
    memory_char_budget: int = DEFAULT_MEMORY_CHAR_BUDGET,
) -> list[ChatTurn]:
    """Assemble the chat turns for one question.

    Shared by the streaming thread route and the stateless answer route so the
    two cannot drift apart.

    The layout is built around one constraint: everything before the final user
    turn should be identical from one turn to the next, because a prompt cache
    matches on a prefix and dies at the first byte that differs.

        system   role, memory, summary, rules   stable across turns
        history  the surviving verbatim turns   append-only
        user     evidence + question            the only volatile part

    So the rules describe both the evidence and no-evidence cases rather than
    switching between two variants, and the evidence rides on the user turn
    rather than sitting in the system message. That placement pays twice: it
    keeps the prefix cacheable, and it puts the extracts at the end of the
    request, where attention is high, instead of the middle, which is the part
    a model skips.

    Within the system message the order is still load-bearing: the rules come
    after the preferences so they visibly override them.
    """
    stamp = now or datetime.now(UTC)
    sections = _memory_sections(
        memory or [],
        now=stamp,
        max_items=memory_max_items,
        char_ceiling=memory_char_budget,
    )
    if summary:
        sections.append(SUMMARY_SECTION.format(summary=summary))
        # Only asked for when there are items to name, so a short chat pays
        # no tokens for an instruction that could not apply.
        sections.append(USED_MARKER_INSTRUCTION)
    rendered = "\n\n".join(sections)
    if rendered:
        rendered += "\n\n"

    if evidence:
        block = "\n\n".join(
            f"[{position}] {item.breadcrumb or item.chunk_id}\n{item.source_text}"
            for position, item in enumerate(evidence, start=1)
        )
        user_content = EVIDENCE_BLOCK.format(evidence=block, question=question)
    else:
        # The bare question, with no wrapper at all: an empty evidence header
        # would read as "extracts were retrieved and they were empty".
        user_content = question

    return [
        ChatTurn(role="system", content=SYSTEM_PROMPT.format(context_sections=rendered)),
        *(history or []),
        ChatTurn(role="user", content=user_content),
    ]


@runtime_checkable
class Answerer(Protocol):
    def answer(
        self,
        question: str,
        evidence: list[AnswerEvidence],
        *,
        sticky_key: str | None = None,
    ) -> Answer: ...


class MockAnswerer:
    """Return the retrieved evidence verbatim instead of calling a model.

    Keeping generation out of the loop makes retrieval changes observable
    without spending tokens on every experiment.
    """

    def answer(
        self,
        question: str,
        evidence: list[AnswerEvidence],
        *,
        sticky_key: str | None = None,
    ) -> Answer:
        if not evidence:
            return Answer(
                text=f"No evidence was retrieved for {question!r}.",
                citations=[],
            )
        sections = [
            f"[{position}] {item.breadcrumb or item.chunk_id}\n{item.source_text}"
            for position, item in enumerate(evidence, start=1)
        ]
        body = "\n\n".join(sections)
        return Answer(
            text=f"Retrieved {len(evidence)} evidence chunks for {question!r}:\n\n{body}",
            citations=[item.chunk_id for item in evidence],
        )


class LLMAnswerer:
    """Generate a grounded answer through a chat provider.

    Citations list every evidence chunk placed in the prompt rather than
    parsing bracketed markers out of the generated text. That keeps the
    citation set honest about what the model was actually shown.
    """

    def __init__(self, chat: ChatCompleter, *, max_tokens: int = 2048) -> None:
        self._chat = chat
        self._max_tokens = max_tokens

    def answer(
        self,
        question: str,
        evidence: list[AnswerEvidence],
        *,
        sticky_key: str | None = None,
    ) -> Answer:
        if not evidence:
            return Answer(
                text=NO_EVIDENCE_RESPONSE.format(question=question), citations=[]
            )
        text = self._chat.complete(
            build_turns(question, evidence),
            max_tokens=self._max_tokens,
            sticky_key=sticky_key,
        )
        return Answer(text=text, citations=[item.chunk_id for item in evidence])
