"""The LangChain-backed generation adapter.

What matters here is the seam, not the agent loop itself: the loop is
LangChain's to keep correct, while sticky keys, usage accounting, error
sanitization and "only the answer text reaches the user" are ours.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.config import RouterConfig
from rag_ingestion.core.agent import BaseAgent
from rag_ingestion.providers.registry import ProviderRequestError


TURNS = [
    ChatTurn(role="system", content="You are helpful."),
    ChatTurn(role="user", content="Hi, I'm Dat"),
]


class ScriptedModel(BaseChatModel):
    """Replays a fixed list of assistant messages, tool calls included."""

    responses: list = []
    index: int = 0
    usage: dict | None = None

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.responses[min(self.index, len(self.responses) - 1)]
        object.__setattr__(self, "index", self.index + 1)
        if self.usage is not None:
            message = message.model_copy(update={"usage_metadata": self.usage})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self


class ExplodingModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "exploding"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("provider said: Bearer sk-secret-key is invalid")

    def bind_tools(self, tools, **kwargs):
        return self


def _routers(keys: list[str] | None = None) -> dict[str, RouterConfig]:
    return {
        "openrouter": RouterConfig(
            base_url="https://openrouter.ai/api/v1", api_keys=keys or ["key-a"]
        )
    }


def _adapter(model: BaseChatModel, keys: list[str] | None = None) -> BaseAgent:
    return BaseAgent(
        _routers(keys), "test/model", model_factory=lambda: model
    )


def test_adapter_satisfies_the_chat_protocol() -> None:
    assert isinstance(_adapter(ScriptedModel(responses=[AIMessage(content="hi")])), ChatCompleter)


def test_stream_yields_only_the_answer_text() -> None:
    adapter = _adapter(ScriptedModel(responses=[AIMessage(content="Hello there.")]))

    assert "".join(adapter.stream(TURNS, max_tokens=64)) == "Hello there."


def test_a_tool_call_runs_and_its_result_never_reaches_the_user() -> None:
    saved: list[tuple[str, str]] = []

    @tool
    def save_user_memory(key: str, value: str) -> str:
        """Remember a durable fact about the user."""
        saved.append((key, value))
        return "saved"

    adapter = _adapter(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "save_user_memory",
                            "args": {"key": "nickname", "value": "Dat"},
                            "id": "c1",
                        }
                    ],
                ),
                AIMessage(content="Nice to meet you, Dat."),
            ]
        )
    )

    text = "".join(
        adapter.stream(TURNS, max_tokens=64, tools=[save_user_memory])
    )

    assert saved == [("nickname", "Dat")]
    assert text == "Nice to meet you, Dat."
    assert "saved" not in text, "tool results are model input, not user output"


def test_usage_is_reported_with_the_accounting_table_key_names() -> None:
    seen: list[dict] = []
    adapter = _adapter(
        ScriptedModel(
            responses=[AIMessage(content="hi")],
            usage={"input_tokens": 138, "output_tokens": 16, "total_tokens": 154},
        )
    )

    "".join(adapter.stream(TURNS, max_tokens=64, on_usage=seen.append))

    assert seen == [
        {"prompt_tokens": 138, "completion_tokens": 16, "total_tokens": 154}
    ]


def test_provider_failures_are_sanitized() -> None:
    adapter = _adapter(ExplodingModel())

    with pytest.raises(ProviderRequestError) as raised:
        "".join(adapter.stream(TURNS, max_tokens=64))

    assert "sk-secret-key" not in str(raised.value)
    assert str(raised.value) == "Chat provider request failed"


def test_one_thread_stays_on_one_key() -> None:
    """Sticky rotation keeps a conversation on one account, so the provider's
    prompt cache keeps hitting across its turns."""
    adapter = BaseAgent(_routers(["key-a", "key-b", "key-c"]), "test/model")

    pool = adapter._pools["openrouter"]
    first = pool.key_for("thread-1")
    again = pool.key_for("thread-1")
    other = pool.key_for("thread-2")

    assert first == again
    assert {first, other} <= {"key-a", "key-b", "key-c"}


# --- router routing --------------------------------------------------------


def _two_router_adapter() -> BaseAgent:
    return BaseAgent(
        {
            "openrouter": RouterConfig(
                base_url="https://openrouter.ai/api/v1", api_keys=["or-key"]
            ),
            "orcarouter": RouterConfig(
                base_url="https://api.orcarouter.ai/v1", api_keys=["orca-key"]
            ),
        },
        "test/model",
    )


def test_a_call_reaches_the_router_it_named() -> None:
    """`router` picks the base URL and the key pool together; naming none
    stays on the default. Inspected on the built client, no network."""
    adapter = _two_router_adapter()

    on_default = adapter._model_for(None, 64)
    on_orca = adapter._model_for(None, 64, model="deepseek/deepseek-v4-pro-free", router="orcarouter")

    assert on_default.openai_api_base == "https://openrouter.ai/api/v1"
    assert on_orca.openai_api_base == "https://api.orcarouter.ai/v1"


def test_a_keyless_router_is_refused_per_call_not_at_construction() -> None:
    """Configuring a router without keys must not sink the whole agent; only
    a call that names it fails, and as the sanitized error type."""
    adapter = BaseAgent(
        {
            "openrouter": RouterConfig(
                base_url="https://openrouter.ai/api/v1", api_keys=["or-key"]
            ),
            "orcarouter": RouterConfig(base_url="https://api.orcarouter.ai/v1"),
        },
        "test/model",
    )

    assert adapter._model_for(None, 64) is not None
    with pytest.raises(ProviderRequestError):
        adapter._model_for(None, 64, router="orcarouter")


def test_a_keyless_default_router_fails_at_construction() -> None:
    """No keys on the default router means no call can ever succeed, so the
    constructor is the right place to hear about it."""
    with pytest.raises(ValueError):
        BaseAgent(
            {"openrouter": RouterConfig(base_url="https://openrouter.ai/api/v1")},
            "test/model",
        )


def test_complete_reuses_the_streaming_path() -> None:
    adapter = _adapter(ScriptedModel(responses=[AIMessage(content="one answer")]))

    assert adapter.complete(TURNS, max_tokens=64) == "one answer"


# --- provider portability ------------------------------------------------------


def test_the_model_is_built_through_the_provider_registry() -> None:
    """`init_chat_model` is what makes the provider a setting rather than an
    import: the same call returns an Anthropic, Google or Ollama client."""
    from rag_ingestion.core.llm import build_chat_model

    model = build_chat_model(
        model="openrouter/free",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        provider="openai",
        max_tokens=64,
    )

    assert model.__class__.__name__ == "ChatOpenAI"


def test_identical_requests_reuse_one_client() -> None:
    """Rebuilding a client per turn rebuilds its connection pool with it."""
    from rag_ingestion.core.llm import build_chat_model

    first = build_chat_model(model="m", api_key="k", base_url="https://x/v1")
    again = build_chat_model(model="m", api_key="k", base_url="https://x/v1")
    other_key = build_chat_model(model="m", api_key="k2", base_url="https://x/v1")

    assert first is again
    assert first is not other_key, "sticky rotation must not share a credential"
