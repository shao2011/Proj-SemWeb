"""Chat models for the experiments: the system's `BaseAgent`, pinned to one model
at temperature 0, with token usage and latency recorded per call."""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rag_ingestion.chat import ChatTurn
from rag_ingestion.config import RuntimeConfig
from rag_ingestion.core.agent import BaseAgent
from rag_ingestion.model_catalog import load_catalog

from eval.settings import TEMPERATURE

_RETRY_ATTEMPTS = 5


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float


def _temperature_zero_factory(
    model_id: str, base_url: str, keys: list[str], max_tokens: int, timeout: float
) -> Callable[[], Any]:
    from langchain.chat_models import init_chat_model

    rotation = itertools.cycle(keys)

    def factory() -> Any:
        return init_chat_model(
            model_id,
            model_provider="openai",
            api_key=next(rotation),
            base_url=base_url,
            temperature=TEMPERATURE,
            max_completion_tokens=max_tokens,
            timeout=timeout,
            stream_usage=True,
        )

    return factory


class ChatClient:
    """One model behind the same `ChatCompleter` interface the system uses.

    `agent` can be handed to the system's summarizer, so summaries are written
    by the answer model.
    """

    def __init__(
        self,
        model_id: str,
        router: str,
        *,
        config: RuntimeConfig | None = None,
        max_tokens: int | None = None,
        model_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model_id
        self.router = router
        self._sleep = sleep
        if max_tokens is None:
            spec = next((m for m in load_catalog().models if m.id == model_id), None)
            max_tokens = spec.max_output_tokens if spec else 2048
        self.max_tokens = max_tokens
        if model_factory is None:
            active = config or RuntimeConfig.from_environment()
            gateway = active.llm.routers[router]
            if not gateway.api_keys:
                raise SystemExit(f"No API key for router {router!r}")
            model_factory = _temperature_zero_factory(
                model_id, gateway.base_url, gateway.api_keys, max_tokens, 120.0
            )
        self._model_factory = model_factory
        self.agent = BaseAgent({}, model_id, default_router=router, model_factory=model_factory)

    def langchain_model(self) -> Any:
        """The same pinned model as a LangChain chat model (Mem0, LangMem)."""
        return self._model_factory()

    def complete(self, turns: list[ChatTurn]) -> Completion:
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            usage: dict[str, object] = {}
            started = time.perf_counter()
            try:
                text = self.agent.complete(
                    turns, max_tokens=self.max_tokens, on_usage=usage.update
                )
            except Exception:
                if attempt == _RETRY_ATTEMPTS:
                    raise
                self._sleep(min(2**attempt, 60))
                continue
            return Completion(
                text=text,
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                seconds=time.perf_counter() - started,
            )
        raise AssertionError("unreachable")
