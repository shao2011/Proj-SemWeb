"""The one way this codebase talks to an LLM.

Why the standard agent loop rather than a hand-rolled one: tool calling is a
maintained primitive, tools written as LangChain tools stay usable if the
harness grows (deepagents builds on this same stack), and the loop's edge
cases -- parallel calls, malformed arguments, round limits -- are somebody
else's job to keep correct.

What this adapter keeps from the direct OpenRouter client, because none of it
is LangChain's concern:

* **Sticky key rotation.** One thread stays on one account so the provider's
  prompt cache keeps hitting; the model object is therefore resolved per call
  (through `core.llm`, which caches them), since a LangChain chat model holds
  a single credential.
* **Usage accounting.** `usage_metadata` on the streamed chunks feeds the same
  callback the raw client used.
* **Sanitized errors.** Provider exceptions become `ProviderRequestError`
  without their text: an upstream body can echo the request, and the request
  carries the API key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from rag_ingestion.chat import ChatTurn
from rag_ingestion.config import RouterConfig
from rag_ingestion.core.llm import DEFAULT_LLM_PROVIDER, build_chat_model
from rag_ingestion.logger import get_logger
from rag_ingestion.providers.keys import StickyKeyPool
from rag_ingestion.providers.registry import ProviderRequestError

_log = get_logger(__name__)

#: Cap on tool-execution rounds per turn. Memory saves need one; this is the
#: backstop against a model that keeps calling tools instead of answering.
_RECURSION_LIMIT = 8


class BaseAgent:
    """`ChatCompleter` backed by `create_agent`, with tools per call.

    Every LLM node runs through this: generation with memory tools,
    understanding, summarization. They differ only in model, prompt and
    whether they pass tools, so a second client would only be a second place
    for key rotation, usage accounting and error handling to drift.
    """

    def __init__(
        self,
        routers: dict[str, RouterConfig],
        model: str,
        *,
        default_router: str = "openrouter",
        provider: str = DEFAULT_LLM_PROVIDER,
        timeout: float = 120.0,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self._default_router = default_router
        self._base_urls = {name: config.base_url for name, config in routers.items()}
        # A keyless router gets no pool rather than failing construction: it
        # is a known destination that is not usable yet, and the API layer
        # hides its models from the picker. Sticky mapping is per pool, so a
        # thread keeps its key on each router independently.
        self._pools = {
            name: StickyKeyPool(config.api_keys)
            for name, config in routers.items()
            if config.api_keys
        }
        if model_factory is None and default_router not in self._pools:
            raise ValueError(f"Default router {default_router!r} has no API keys")
        self._provider = provider
        self._timeout = timeout
        self._model_factory = model_factory

    def _model_for(
        self,
        sticky_key: str | None,
        max_tokens: int,
        model: str | None = None,
        router: str | None = None,
    ) -> Any:
        if self._model_factory is not None:
            return self._model_factory()
        name = router or self._default_router
        pool = self._pools.get(name)
        if pool is None:
            # Raised rather than rerouted: by the time a call lands here the
            # route handler has already had its chance to fall back, so a
            # missing pool is a wiring bug and rerouting would bury it.
            raise ProviderRequestError(f"Chat router {name!r} has no API keys")
        return build_chat_model(
            model=model or self.model,
            api_key=pool.key_for(sticky_key),
            base_url=self._base_urls[name],
            provider=self._provider,
            timeout=self._timeout,
            max_tokens=max_tokens,
        )

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: Callable[[dict[str, object]], None] | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> str:
        return "".join(
            self.stream(
                turns,
                max_tokens=max_tokens,
                sticky_key=sticky_key,
                on_usage=on_usage,
                model=model,
                router=router,
            )
        )

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: Callable[[dict[str, object]], None] | None = None,
        tools: list[Any] | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> Iterator[str]:
        from langchain.agents import create_agent

        system = [turn.content for turn in turns if turn.role == "system"]
        conversation = [
            {"role": turn.role, "content": turn.content}
            for turn in turns
            if turn.role != "system"
        ]
        agent = create_agent(
            self._model_for(sticky_key, max_tokens, model, router),
            list(tools or []),
            system_prompt="\n\n".join(system) or None,
        )
        try:
            for chunk, metadata in agent.stream(
                {"messages": conversation},
                stream_mode="messages",
                config={"recursion_limit": _RECURSION_LIMIT},
            ):
                usage = getattr(chunk, "usage_metadata", None)
                if usage and on_usage is not None:
                    _log.info("LLM usage for model=%s: %s", self.model, usage)
                    on_usage(_as_provider_usage(usage))
                # Only the answering node's text reaches the user: tool results
                # are inputs to the next model round, not part of the reply.
                if metadata.get("langgraph_node") != "model":
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    yield content
        except Exception as error:
            _log.exception("Agent generation failed for model=%s", self.model)
            raise ProviderRequestError("Chat provider request failed") from error


def _as_provider_usage(usage: dict[str, Any]) -> dict[str, object]:
    """LangChain's usage keys, renamed to the ones the accounting table uses."""
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
