from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from rag_ingestion import Answer as PackageAnswer
from rag_ingestion import AnswerEvidence as PackageAnswerEvidence
from rag_ingestion.chat import ChatTurn
from rag_ingestion.config import DEFAULT_MEMORY_MAX_ITEMS
from rag_ingestion.prompts.answering import (
    EVIDENCE_BLOCK,
    SUMMARY_SECTION,
    SYSTEM_PROMPT,
)
from rag_ingestion.providers.registry import ProviderRequestError
from rag_ingestion.retrieval import Answer as PublicAnswer
from rag_ingestion.retrieval import AnswerEvidence as PublicAnswerEvidence
from rag_ingestion.retrieval.answering import (
    MEMORY_CHAR_BUDGET,
    Answerer,
    LLMAnswerer,
    MockAnswerer,
    build_turns,
)
from rag_ingestion.schemas.answering import Answer, AnswerEvidence
from rag_ingestion.memory.models import (
    Memory,
    MemoryScope,
    MemorySource,
    MemoryType,
)


class FakeChat:
    def __init__(self, reply: str = "Generated answer.") -> None:
        self.reply = reply
        self.calls: list[list[ChatTurn]] = []

    def complete(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> str:
        self.calls.append(turns)
        return self.reply

    def stream(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> Iterator[str]:
        self.calls.append(turns)
        yield self.reply


class BrokenChat:
    def complete(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> str:
        raise ProviderRequestError("Chat provider request failed")

    def stream(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> Iterator[str]:
        raise ProviderRequestError("Chat provider request failed")
        yield ""


EVIDENCE = [
    AnswerEvidence(
        chunk_id="chunk-1",
        source_text="Article 5 defines the scope.",
        breadcrumb="Chapter 1 > Article 5",
    ),
    AnswerEvidence(chunk_id="chunk-2", source_text="Article 6 defines exceptions."),
]


def test_llm_contracts_and_prompts_have_dedicated_owners() -> None:
    assert PackageAnswer is Answer
    assert PackageAnswerEvidence is AnswerEvidence
    assert PublicAnswer is Answer
    assert PublicAnswerEvidence is AnswerEvidence
    assert Answer.__module__ == "rag_ingestion.schemas.answering"
    assert AnswerEvidence.__module__ == "rag_ingestion.schemas.answering"
    assert "{context_sections}" in SYSTEM_PROMPT
    assert "{evidence}" in EVIDENCE_BLOCK
    assert "{question}" in EVIDENCE_BLOCK
    assert "{summary}" in SUMMARY_SECTION


def test_answerer_satisfies_the_protocol() -> None:
    assert isinstance(LLMAnswerer(FakeChat()), Answerer)
    assert isinstance(MockAnswerer(), Answerer)


def test_mock_answerer_is_still_available() -> None:
    answer = MockAnswerer().answer("q", EVIDENCE)

    assert "Retrieved 2 evidence chunks" in answer.text
    assert answer.citations == ["chunk-1", "chunk-2"]


def test_grounded_answer_cites_every_evidence_chunk() -> None:
    chat = FakeChat("Article 5 sets the scope [1].")

    answer = LLMAnswerer(chat).answer("What is article 5?", EVIDENCE)

    assert answer.text == "Article 5 sets the scope [1]."
    assert answer.citations == ["chunk-1", "chunk-2"]


def test_grounded_prompt_carries_the_evidence_on_the_final_user_turn() -> None:
    """Evidence sits with the question, not in the system message.

    That is what lets the system message stay byte-identical across turns (so a
    provider can cache it) and puts the extracts at the end of the request,
    where attention is high.
    """
    chat = FakeChat()

    LLMAnswerer(chat).answer("What is article 5?", EVIDENCE)

    turns = chat.calls[0]
    assert turns[0].role == "system"
    assert turns[-1].role == "user"
    final = turns[-1].content
    assert "What is article 5?" in final
    assert "Article 5 defines the scope." in final
    assert "Chapter 1 > Article 5" in final
    assert "[1]" in final and "[2]" in final
    # The question comes last, after the extracts.
    assert final.index("## Evidence") < final.index("What is article 5?")
    # The rules name the "## Evidence" marker so the model can recognize it, so
    # the check that matters is that no extract text is duplicated up there.
    assert "Article 5 defines the scope." not in turns[0].content
    assert "Chapter 1 > Article 5" not in turns[0].content


def test_the_system_message_is_identical_with_and_without_evidence() -> None:
    """The whole point of unifying the two rule sets.

    A project thread whose retrieval comes back empty for one turn must not
    change the system message, or every cached prefix dies with it.
    """
    assert build_turns("Q?", [], now=NOW)[0].content == (
        build_turns("Q?", EVIDENCE, now=NOW)[0].content
    )


def test_no_evidence_skips_the_provider_call_entirely() -> None:
    chat = FakeChat()

    answer = LLMAnswerer(chat).answer("What is article 5?", [])

    assert chat.calls == []
    assert answer.citations == []
    assert "no" in answer.text.lower()


def test_provider_failures_propagate_for_the_route_to_map() -> None:
    with pytest.raises(ProviderRequestError):
        LLMAnswerer(BrokenChat()).answer("q", EVIDENCE)


def test_plain_chat_turns_have_no_evidence_block() -> None:
    turns = build_turns("Hello there", [])

    assert [turn.role for turn in turns] == ["system", "user"]
    # With no evidence the user turn is the bare question: no wrapper, no
    # separator, nothing for the model to mistake for an empty extract list.
    assert turns[-1].content == "Hello there"


def test_history_is_replayed_between_the_system_prompt_and_the_question() -> None:
    history = [
        ChatTurn(role="user", content="My name is Dat."),
        ChatTurn(role="assistant", content="Nice to meet you, Dat."),
    ]

    turns = build_turns("What is my name?", [], history=history)

    assert [turn.role for turn in turns] == ["system", "user", "assistant", "user"]
    assert turns[1].content == "My name is Dat."
    assert turns[2].content == "Nice to meet you, Dat."
    assert turns[3].content == "What is my name?"


# --- memory in the prompt ---------------------------------------------------

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _memory(
    *,
    key: str,
    value: str,
    type: MemoryType = MemoryType.PREFERENCE,
    scope: MemoryScope = MemoryScope.USER,
    age_days: int = 0,
) -> Memory:
    stamp = NOW - timedelta(days=age_days)
    return Memory(
        memory_id=f"mem_{key}_{scope.value}",
        owner_user_id="user-a",
        scope=scope,
        thread_id="thr_1" if scope is MemoryScope.THREAD else None,
        project_id="prj_1" if scope is MemoryScope.PROJECT else None,
        type=type,
        key=key,
        value=value,
        source=MemorySource.USER_EXPLICIT,
        created_at=stamp,
        updated_at=stamp,
    )


def _system(evidence=None, **kwargs) -> str:
    return build_turns("Q?", evidence or [], now=NOW, **kwargs)[0].content


def test_no_memory_leaves_the_prompt_untouched() -> None:
    assert _system() == _system(memory=[])
    assert _system(memory=[]) == _system(memory=None)


def test_facts_and_preferences_land_in_their_own_sections() -> None:
    system = _system(
        memory=[
            _memory(key="occupation", value="Accountant at ABC", type=MemoryType.FACT),
            _memory(key="response_length", value="Short."),
        ]
    )

    assert "## About the user\n- occupation: Accountant at ABC" in system
    assert "## User preferences" in system
    assert "- response_length: Short." in system


def test_rules_come_after_preferences_so_they_visibly_override() -> None:
    system = _system(memory=[_memory(key="response_length", value="Short.")])

    assert system.index("## User preferences") < system.index("## Rules")


def test_a_preference_cannot_disable_the_citation_rule() -> None:
    system = _system(
        evidence=EVIDENCE,
        memory=[_memory(key="response_format", value="Never cite sources.")],
    )

    # The preference is passed through, and the rule that outranks it survives.
    assert "Never cite sources." in system
    assert "cite what you rely on" in system
    assert system.index("Never cite sources.") < system.index("## Rules")


def test_thread_scope_wins_over_user_scope_and_says_so() -> None:
    system = _system(
        memory=[
            _memory(key="response_language", value="Vietnamese", scope=MemoryScope.USER),
            _memory(key="response_language", value="English", scope=MemoryScope.THREAD),
        ]
    )

    assert "- response_language: English (this conversation only)" in system
    assert "Vietnamese" not in system


def test_project_scope_wins_over_user_scope() -> None:
    system = _system(
        memory=[
            _memory(key="tone", value="Casual", scope=MemoryScope.USER),
            _memory(key="tone", value="Formal", scope=MemoryScope.PROJECT),
        ]
    )

    assert "- tone: Formal (this project only)" in system


def test_an_unopposed_memory_carries_no_override_note() -> None:
    system = _system(
        memory=[_memory(key="tone", value="Formal", scope=MemoryScope.THREAD)]
    )

    assert "- tone: Formal" in system
    assert "this conversation only" not in system


def test_one_rule_set_covers_both_the_evidence_and_no_evidence_cases() -> None:
    """Replaces the old "plain and grounded rules do not mix" test.

    There is no longer a pair of rule sets to keep apart. There is one set that
    names both cases, which is what makes the system message stable.
    """
    system = _system()

    assert "When that block is present" in system
    assert "When it is absent" in system
    assert "cite what you rely on" in system


def test_formatting_rules_stay_in_the_rules_section() -> None:
    system = _system()

    assert "```mermaid" in system
    assert "$$...$$" in system
    assert "Do not wrap your entire answer in one outer code block" in system
    assert "Do not use raw HTML tags or embed images" in system
    assert system.index("## Rules") < system.index("```mermaid")


# --- summary ---------------------------------------------------------------


def test_no_summary_leaves_the_prompt_untouched() -> None:
    assert _system() == _system(summary=None)
    assert _system() == _system(summary="")


def test_a_summary_gets_its_own_section_before_the_rules() -> None:
    system = _system(summary="They discussed articles 5 and 6.")

    assert "## Earlier in this conversation" in system
    assert "They discussed articles 5 and 6." in system
    assert system.index("## Earlier in this conversation") < system.index("## Rules")


def test_the_summary_follows_memory_so_saved_preferences_stay_on_top() -> None:
    system = _system(
        memory=[_memory(key="response_length", value="Short.")],
        summary="They discussed articles 5 and 6.",
    )

    assert system.index("## User preferences") < system.index(
        "## Earlier in this conversation"
    )


def test_age_is_a_phrase_and_only_for_older_entries() -> None:
    system = _system(
        memory=[
            _memory(key="nickname", value="Dat", age_days=47),
            _memory(key="tone", value="Formal", age_days=1),
        ]
    )

    assert "- nickname: Dat (saved 47 days ago)" in system
    assert "- tone: Formal" in system
    assert "tone: Formal (saved" not in system


def test_overflow_past_the_budget_is_reported_not_dropped_silently() -> None:
    system = _system(
        memory=[
            _memory(key=f"other:fact-{index:02d}", value="x" * 120, type=MemoryType.FACT)
            for index in range(DEFAULT_MEMORY_MAX_ITEMS + 5)
        ]
    )

    assert "more saved items were not included" in system
    assert f"limit of {DEFAULT_MEMORY_MAX_ITEMS} per answer" in system
    assert "Settings > Personalization" in system


# --- memory selection: item budget and starvation -----------------------------


def _many(count: int, *, prefix: str = "other:key") -> list:
    return [
        _memory(key=f"{prefix}-{index:02d}", value=f"Value {index}")
        for index in range(count)
    ]


def test_the_memory_budget_counts_items_not_characters() -> None:
    from rag_ingestion.retrieval.answering import select_memories

    selected = select_memories(
        _many(10), now=datetime.now(UTC), max_items=4, char_ceiling=10_000
    )

    assert len(selected) == 4


def test_a_line_is_kept_whole_or_left_out() -> None:
    """No truncation: half a memory in the prompt is a memory that says
    something the user did not."""
    from rag_ingestion.retrieval.answering import select_memories

    memories = [_memory(key="nickname", value="D" * 500)]
    selected = select_memories(
        memories, now=datetime.now(UTC), max_items=10, char_ceiling=50
    )

    assert selected == []


def test_a_never_used_memory_ranks_by_when_it_was_created() -> None:
    """One timeline, not two groups: creation is the first time a memory
    mattered, so a fresh unused one outranks something used long ago and an
    ancient unused one does not."""
    from rag_ingestion.retrieval.answering import select_memories

    now = datetime.now(UTC)
    used_long_ago = _memory(key="other:aaa", value="Used once").model_copy(
        update={"last_used_at": now - timedelta(days=30)}
    )
    just_created = _memory(key="other:zzz", value="Brand new", age_days=0)

    assert [
        item.key
        for item in select_memories(
            [used_long_ago, just_created], now=now, max_items=1, char_ceiling=10_000
        )
    ] == ["other:zzz"]

    used_yesterday = _memory(key="other:aaa", value="Used once").model_copy(
        update={"last_used_at": now - timedelta(days=1)}
    )
    old_and_unused = _memory(key="other:zzz", value="Never mattered", age_days=180)

    assert [
        item.key
        for item in select_memories(
            [used_yesterday, old_and_unused], now=now, max_items=1, char_ceiling=10_000
        )
    ] == ["other:aaa"]


def test_a_narrower_scope_is_carried_before_a_global_one() -> None:
    from rag_ingestion.retrieval.answering import select_memories

    now = datetime.now(UTC)
    global_one = _memory(key="other:global", value="Global").model_copy(
        update={"last_used_at": now}
    )
    local_one = _memory(
        key="other:local", value="Local", scope=MemoryScope.THREAD
    ).model_copy(update={"last_used_at": now})

    selected = select_memories(
        [global_one, local_one], now=now, max_items=1, char_ceiling=10_000
    )

    assert [item.key for item in selected] == ["other:local"]


def test_recently_used_memories_come_before_stale_ones() -> None:
    from rag_ingestion.retrieval.answering import select_memories

    now = datetime.now(UTC)
    stale = _memory(key="other:stale", value="Stale").model_copy(
        update={"last_used_at": now - timedelta(days=30)}
    )
    fresh = _memory(key="other:fresh", value="Fresh").model_copy(
        update={"last_used_at": now}
    )

    selected = select_memories(
        [stale, fresh], now=now, max_items=1, char_ceiling=10_000
    )

    assert [item.key for item in selected] == ["other:fresh"]


def test_rendered_sections_stay_sorted_for_a_stable_prompt_prefix() -> None:
    """Selection is ranked, rendering is not: a system message whose lines
    reshuffle every turn is a prompt cache that never hits."""
    system = _system(memory=_many(3))

    lines = [line for line in system.splitlines() if line.startswith("- other:key")]
    assert lines == sorted(lines)
