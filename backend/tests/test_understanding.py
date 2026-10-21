from collections.abc import Iterator

import pytest

from rag_ingestion.chat import ChatTurn
from rag_ingestion.providers.registry import ProviderRequestError
from rag_ingestion.schemas.understanding import MemoryCandidate, Understanding
from rag_ingestion.memory.models import MemoryScope, MemoryType
from rag_ingestion.understanding import LLMUnderstander, Understander


class FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[ChatTurn]] = []
        self.picked: list[tuple[str | None, str | None]] = []
        self.model = "cheap/model"

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
        return self.reply

    def stream(
        self, turns: list[ChatTurn], *, max_tokens: int, sticky_key: str | None = None
    ) -> Iterator[str]:
        self.calls.append(turns)
        yield self.reply


class BrokenChat:
    model = "cheap/model"

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


def test_satisfies_the_understander_protocol() -> None:
    assert isinstance(LLMUnderstander(FakeChat("{}")), Understander)


def test_the_sessions_model_rides_through_to_the_chat_call() -> None:
    """Understanding runs on whatever the user is chatting with; the picked
    model and its router reach the provider call unchanged."""
    chat = FakeChat('{"rewritten_query": null, "memory": []}')

    LLMUnderstander(chat).understand(
        "q", "", model="deepseek/deepseek-v4-flash-free", router="orcarouter"
    )

    assert chat.picked == [("deepseek/deepseek-v4-flash-free", "orcarouter")]


def test_valid_json_round_trips() -> None:
    chat = FakeChat(
        '{"rewritten_query": "Who does Article 5 apply to?", '
        '"memory": [{"type": "fact", "key": "occupation", '
        '"value": "accountant", "suggested_scope": "user"}]}'
    )
    result = LLMUnderstander(chat).understand("who does it apply to?", "prior turn")

    assert result.rewritten_query == "Who does Article 5 apply to?"
    assert result.memory == [
        MemoryCandidate(
            type=MemoryType.FACT, key="occupation", value="accountant"
        )
    ]
    system, user = chat.calls[0]
    assert system.role == "system"
    assert user.role == "user"
    assert "prior turn" in user.content
    assert "who does it apply to?" in user.content


def test_null_rewritten_query_and_empty_memory() -> None:
    chat = FakeChat('{"rewritten_query": null, "memory": []}')
    result = LLMUnderstander(chat).understand("Article 5 Decree 15/2020 says what?", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_markdown_fenced_json_is_tolerated() -> None:
    chat = FakeChat('```json\n{"rewritten_query": null, "memory": null}\n```')
    result = LLMUnderstander(chat).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_malformed_json_falls_back_without_raising() -> None:
    chat = FakeChat("not json at all")
    result = LLMUnderstander(chat).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_non_object_json_falls_back() -> None:
    chat = FakeChat('["not", "an", "object"]')
    result = LLMUnderstander(chat).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_non_string_rewritten_query_falls_back() -> None:
    chat = FakeChat('{"rewritten_query": 42, "memory": []}')
    result = LLMUnderstander(chat).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_non_list_memory_falls_back() -> None:
    chat = FakeChat('{"rewritten_query": null, "memory": "not a list"}')
    result = LLMUnderstander(chat).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_provider_error_falls_back_without_raising() -> None:
    result = LLMUnderstander(BrokenChat()).understand("q", "")

    assert result == Understanding(rewritten_query=None, memory=[])


def test_pin_type_is_dropped_per_candidate_not_fatal() -> None:
    chat = FakeChat(
        '{"rewritten_query": null, "memory": ['
        '{"type": "pin", "key": "other:favourite-doc", "value": "x", '
        '"suggested_scope": "user"}, '
        '{"type": "fact", "key": "nickname", "value": "Dat", '
        '"suggested_scope": "thread"}]}'
    )
    result = LLMUnderstander(chat).understand("q", "")

    assert result.memory == [
        MemoryCandidate(type=MemoryType.FACT, key="nickname", value="Dat")
    ]


def test_invalid_key_is_dropped_per_candidate_not_fatal() -> None:
    chat = FakeChat(
        '{"rewritten_query": null, "memory": ['
        '{"type": "fact", "key": "not_a_real_key", "value": "x", '
        '"suggested_scope": "user"}, '
        '{"type": "preference", "key": "response_length", "value": "short", '
        '"suggested_scope": "user"}]}'
    )
    result = LLMUnderstander(chat).understand("q", "")

    assert result.memory == [
        MemoryCandidate(
            type=MemoryType.PREFERENCE, key="response_length", value="short"
        )
    ]


def test_memory_capped_at_three_even_when_the_model_sends_more() -> None:
    items = ",".join(
        f'{{"type": "fact", "key": "other:item-{i}", "value": "v{i}", '
        f'"suggested_scope": "thread"}}'
        for i in range(5)
    )
    chat = FakeChat(f'{{"rewritten_query": null, "memory": [{items}]}}')
    result = LLMUnderstander(chat).understand("q", "")

    assert len(result.memory) == 3
    assert [candidate.key for candidate in result.memory] == [
        "other:item-0",
        "other:item-1",
        "other:item-2",
    ]


def test_memory_candidate_rejects_pin_type() -> None:
    with pytest.raises(ValueError):
        MemoryCandidate(type="pin", key="other:x", value="v")


def test_a_stale_suggested_scope_does_not_lose_the_candidate() -> None:
    """The field was removed from the schema; models may still send it.

    Forbidding extras would drop the whole candidate over a key nothing reads.
    """
    chat = FakeChat(
        '{"rewritten_query": null, "memory": ['
        '{"type": "fact", "key": "nickname", "value": "Dat", '
        '"suggested_scope": "user"}]}'
    )

    result = LLMUnderstander(chat).understand("q", "")

    assert result.memory == [
        MemoryCandidate(type=MemoryType.FACT, key="nickname", value="Dat")
    ]
