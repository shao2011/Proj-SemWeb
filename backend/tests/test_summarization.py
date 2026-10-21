from collections.abc import Iterator

from rag_ingestion.chat import ChatTurn
from rag_ingestion.providers.registry import ProviderRequestError
from rag_ingestion.summary import LLMSummarizer, Summarizer, parse_items


class ScriptedChat:
    """Returns one scripted reply per call, recording what it was asked."""

    model = "test-model"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatTurn]] = []
        self.picked: list[tuple[str | None, str | None]] = []

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> str:
        self.calls.append(turns)
        self.picked.append((model, router))
        return self.replies.pop(0)

    def stream(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> Iterator[str]:
        yield self.complete(turns, max_tokens=max_tokens, sticky_key=sticky_key)


class BrokenChat:
    model = "test-model"

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> str:
        raise ProviderRequestError("Chat provider request failed")

    def stream(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> Iterator[str]:
        raise ProviderRequestError("Chat provider request failed")
        yield ""


TURNS = [
    ChatTurn(role="user", content="What does article 5 cover?"),
    ChatTurn(role="assistant", content="Article 5 defines the scope."),
]

ADD_A = '{"ops": [{"op": "ADD", "text": "Article 5 defines the scope"}]}'
ADD_B = '{"ops": [{"op": "ADD", "text": "Article 6 sets the penalties"}]}'

# The fold runs two calls: extract facts from the dropped turns, then curate
# those facts against the item list. Every scripted chat below therefore
# needs an extract reply before each curate reply.
FACTS_A = '{"facts": ["Article 5 defines the scope"]}'
FACTS_B = '{"facts": ["Article 6 sets the penalties"]}'
NO_FACTS = '{"facts": []}'


def test_summarizer_satisfies_the_protocol() -> None:
    assert isinstance(LLMSummarizer(ScriptedChat()), Summarizer)


def test_the_sessions_model_rides_through_both_phases() -> None:
    """The fold runs on whatever the user is chatting with, and both the
    extract call and the curate call must say so."""
    chat = ScriptedChat(FACTS_A, ADD_A)

    LLMSummarizer(chat).summarize(
        previous=None,
        turns=TURNS,
        model="deepseek/deepseek-v4-flash-free",
        router="orcarouter",
    )

    assert chat.picked == [
        ("deepseek/deepseek-v4-flash-free", "orcarouter"),
        ("deepseek/deepseek-v4-flash-free", "orcarouter"),
    ]


def test_a_fact_the_model_never_mentions_survives_the_next_fold() -> None:
    """The reason this summarizer exists. Under the old one, fold 2 rewrote
    the whole summary, so a fact fold 2's reply did not restate was gone.
    Here fold 2 only ADDs, and the fold-1 fact must come through verbatim."""
    chat = ScriptedChat(FACTS_A, ADD_A, FACTS_B, ADD_B)
    summarizer = LLMSummarizer(chat)

    first = summarizer.summarize(previous=None, turns=TURNS)
    second = summarizer.summarize(previous=first, turns=TURNS)

    texts = [item.text for item in parse_items(second)]
    assert "Article 5 defines the scope" in texts
    assert "Article 6 sets the penalties" in texts


def test_instructions_ride_the_extract_prompt() -> None:
    """The user's compact hint steers what is worth remembering, which is
    EXTRACT's question. CURATE only files facts, so it stays untouched."""
    chat = ScriptedChat(FACTS_A, ADD_A)
    summarizer = LLMSummarizer(chat)

    summarizer.summarize(
        previous=None, turns=TURNS, instructions="focus on the dates"
    )

    extract_user = chat.calls[0][1].content
    assert "focus on the dates" in extract_user
    curate_user = chat.calls[1][1].content
    assert "focus on the dates" not in curate_user


def test_first_fold_sends_only_the_new_turns() -> None:
    chat = ScriptedChat(FACTS_A, ADD_A)

    result = LLMSummarizer(chat).summarize(previous=None, turns=TURNS)

    assert result == "[s1|u:0|f:0] Article 5 defines the scope"
    user_content = chat.calls[0][1].content
    assert "Current summary items" not in user_content
    assert "What does article 5 cover?" in user_content


def test_the_two_phases_get_disjoint_inputs() -> None:
    """The point of splitting the fold.

    Extract reads only the dropped turns, so it spends its whole attention on
    "what is worth remembering". Curate reads only the distilled facts and the
    item list, so it spends its whole attention on "does this already exist,
    and where does it go". One call doing both did each half worse.
    """
    chat = ScriptedChat(FACTS_B, ADD_B)

    LLMSummarizer(chat).summarize(
        previous="[s1] Article 5 defines the scope", turns=TURNS
    )

    extract, curate = chat.calls[0][1].content, chat.calls[1][1].content
    assert "What does article 5 cover?" in extract
    assert "[s1] Article 5 defines the scope" not in extract
    assert "[s1] Article 5 defines the scope" in curate
    assert "Article 6 sets the penalties" in curate, "the extracted facts ride along"
    assert "What does article 5 cover?" not in curate, "raw turns stay behind"


def test_nothing_worth_remembering_skips_the_second_call() -> None:
    chat = ScriptedChat(NO_FACTS)

    result = LLMSummarizer(chat).summarize(previous="[s1] Kept", turns=TURNS)

    assert result == "[s1] Kept"
    assert len(chat.calls) == 1, "no facts means no curation to do"


def test_a_failed_extract_keeps_the_previous_summary() -> None:
    chat = ScriptedChat("not json at all")

    result = LLMSummarizer(chat).summarize(previous="[s1] Kept", turns=TURNS)

    assert result == "[s1] Kept"
    assert len(chat.calls) == 1, "a broken extract must not reach curation"


def test_roles_are_labelled_so_the_model_can_tell_who_said_what() -> None:
    chat = ScriptedChat(FACTS_A, ADD_A)

    LLMSummarizer(chat).summarize(previous=None, turns=TURNS)

    user_content = chat.calls[0][1].content
    assert "User: What does article 5 cover?" in user_content
    assert "Assistant: Article 5 defines the scope." in user_content


def test_a_prose_summary_from_the_old_format_stays_addressable() -> None:
    """A pre-migration thread holds prose. It must ride along as `s1` so the
    model can UPDATE or DROP it by name later, not vanish on the first fold."""
    chat = ScriptedChat(FACTS_B, ADD_B)

    result = LLMSummarizer(chat).summarize(
        previous="They discussed articles 5 and 6.", turns=TURNS
    )

    texts = [item.text for item in parse_items(result)]
    assert "They discussed articles 5 and 6." in texts
    assert "Article 6 sets the penalties" in texts


def test_an_unknown_id_in_the_delta_deletes_nothing(caplog) -> None:
    chat = ScriptedChat(FACTS_A, '{"ops": [{"op": "DROP", "id": "s41"}]}')

    result = LLMSummarizer(chat).summarize(
        previous="[s1] The only fact", turns=TURNS
    )

    # Text untouched; only the fold counter moved, which is bookkeeping.
    assert result == "[s1|u:0|f:1] The only fact"


def test_a_provider_failure_keeps_the_previous_summary() -> None:
    result = LLMSummarizer(BrokenChat()).summarize(
        previous="[s1] Earlier they covered article 1", turns=TURNS
    )

    assert result == "[s1] Earlier they covered article 1"


def test_a_provider_failure_with_no_previous_summary_returns_none() -> None:
    assert LLMSummarizer(BrokenChat()).summarize(previous=None, turns=TURNS) is None


def test_output_that_is_not_json_keeps_the_previous_summary() -> None:
    chat = ScriptedChat(FACTS_A, "Here is a lovely prose summary of the chat.")

    result = LLMSummarizer(chat).summarize(previous="[s1] Kept", turns=TURNS)

    assert result == "[s1] Kept"


def test_an_empty_delta_keeps_the_previous_summary_without_rewriting_it() -> None:
    chat = ScriptedChat(FACTS_A, '{"ops": []}')

    result = LLMSummarizer(chat).summarize(previous="[s1] Kept", turns=TURNS)

    assert result == "[s1] Kept"


def test_no_turns_means_no_call_and_no_change() -> None:
    chat = ScriptedChat()

    result = LLMSummarizer(chat).summarize(previous="[s1] Unchanged", turns=[])

    assert result == "[s1] Unchanged"
    assert chat.calls == []


def test_eviction_over_the_item_cap_is_deterministic_and_from_the_front() -> None:
    chat = ScriptedChat('{"facts": ["Newest fact"]}', '{"ops": [{"op": "ADD", "text": "Newest fact"}]}')
    summarizer = LLMSummarizer(chat, max_items=2)

    result = summarizer.summarize(
        previous="[s1] Oldest fact\n[s2] Middle fact", turns=TURNS
    )

    texts = [item.text for item in parse_items(result)]
    assert texts == ["Middle fact", "Newest fact"]


def test_eviction_logs_the_text_it_dropped_not_just_the_id(caplog) -> None:
    """The dropped item is gone from the column the moment this returns, so
    the log line is the only remaining record of what was lost."""
    chat = ScriptedChat('{"facts": ["Newest fact"]}', '{"ops": [{"op": "ADD", "text": "Newest fact"}]}')

    with caplog.at_level("INFO"):
        LLMSummarizer(chat, max_items=1).summarize(
            previous="[s1] Deadline moved to Q4", turns=TURNS
        )

    assert "s1" in caplog.text
    assert "Deadline moved to Q4" in caplog.text
