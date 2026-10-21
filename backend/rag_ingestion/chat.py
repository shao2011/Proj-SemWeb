from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

#: Called with the provider's raw usage dict (prompt_tokens/completion_tokens/
#: total_tokens plus provider extras) when a completion reports it.
OnUsage = Callable[[dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    role: Literal["system", "user", "assistant"]
    content: str


@runtime_checkable
class ChatCompleter(Protocol):
    #: Which model this completer sends requests to, for logging what was
    #: actually pushed to the LLM.
    model: str

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: OnUsage | None = None,
        #: Per-call override of the configured model, for the user's picker.
        #: None keeps the completer's own `model`.
        model: str | None = None,
        #: Gateway the override lives on. None keeps the completer's default;
        #: only meaningful together with `model`, since the configured model
        #: is an id on the default router.
        router: str | None = None,
    ) -> str: ...

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: OnUsage | None = None,
        #: LangChain tools the model may call during the turn. Completers
        #: without tool support (understanding, summarization) ignore this.
        tools: list[object] | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> Iterator[str]: ...
