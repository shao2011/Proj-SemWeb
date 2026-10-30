from langchain_core.language_models.fake_chat_models import FakeListChatModel

from rag_ingestion.chat import ChatTurn

from eval.judge import E1_LABELS, UNPARSED, Judge, parse_label, sample
from eval.llm import ChatClient


def _client(*replies: str, fail_first: int = 0) -> ChatClient:
    state = {"calls": 0}

    def factory():
        state["calls"] += 1
        if state["calls"] <= fail_first:
            raise RuntimeError("rate limited")
        return FakeListChatModel(responses=list(replies))

    return ChatClient("fake", "openrouter", max_tokens=64, model_factory=factory, sleep=lambda s: None)


def test_completion_returns_text_and_timing() -> None:
    reply = _client("hello").complete([ChatTurn(role="user", content="hi")])

    assert reply.text == "hello"
    assert reply.seconds >= 0


def test_transient_failures_are_retried() -> None:
    reply = _client("ok", fail_first=2).complete([ChatTurn(role="user", content="hi")])

    assert reply.text == "ok"


def test_sample_is_fixed_by_the_seed_and_ignores_input_order() -> None:
    ids = list(range(159))

    first = sample(ids)
    assert len(first) == 50
    assert sample(list(reversed(ids))) == first
    assert sample(ids, seed=1) != first
    assert sample([1, 2, 3]) == [1, 2, 3]


def test_parse_label_accepts_json_or_a_single_label_word() -> None:
    assert parse_label('{"label": "partial"}', E1_LABELS) == "Partial"
    assert parse_label("The answer is Hallucinated.", E1_LABELS) == "Hallucinated"
    assert parse_label("Correct or Partial?", E1_LABELS) == UNPARSED


def test_judge_uses_the_definitions_and_parses_the_reply() -> None:
    judge = Judge(_client('{"label": "Stale"}'))

    label = judge.e3(question="q", old_value="5 s", new_value="8 s", cited="8 s", answer="5 s")

    assert label == "Stale"
