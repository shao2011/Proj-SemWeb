"""L2 thread summary: the turns that scrolled out of the window, in prose.

Incremental by design. The previous summary is fed back in as input alongside
only the turns that have newly dropped out, so the cost of one summarization
does not grow with the length of the thread.

Runs after the answer has already been streamed, so it is never on the path of
a reply. Any failure keeps the previous summary rather than clearing it:
losing a summary silently discards conversation history, which is worse than
a summary that lags one round behind.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.logger import get_logger
from rag_ingestion.prompts.summarization import (
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_USER_TEMPLATE,
    SUMMARY_USER_TEMPLATE_FIRST,
)

_log = get_logger(__name__)

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System"}


@runtime_checkable
class Summarizer(Protocol):
    def summarize(
        self,
        *,
        previous: str | None,
        turns: list[ChatTurn],
        sticky_key: str | None = None,
    ) -> str | None: ...


def _render(turns: list[ChatTurn]) -> str:
    return "\n".join(
        f"{_ROLE_LABELS.get(turn.role, turn.role)}: {turn.content}" for turn in turns
    )


class LLMSummarizer:
    def __init__(self, chat: ChatCompleter, *, max_tokens: int = 400) -> None:
        self._chat = chat
        self._max_tokens = max_tokens

    def summarize(
        self,
        *,
        previous: str | None,
        turns: list[ChatTurn],
        sticky_key: str | None = None,
    ) -> str | None:
        if not turns:
            # Nothing dropped out since the last run, so there is nothing to
            # fold in and no reason to spend a call.
            return previous
        rendered = _render(turns)
        user_content = (
            SUMMARY_USER_TEMPLATE.format(summary=previous, turns=rendered)
            if previous
            else SUMMARY_USER_TEMPLATE_FIRST.format(turns=rendered)
        )
        try:
            text = self._chat.complete(
                [
                    ChatTurn(role="system", content=SUMMARY_SYSTEM_PROMPT),
                    ChatTurn(role="user", content=user_content),
                ],
                max_tokens=self._max_tokens,
                sticky_key=sticky_key,
            )
            if not text.strip():
                raise ValueError("Summarizer returned empty text")
            return text.strip()
        except Exception as error:
            # Deliberately broad, matching UnderstandMessage: a provider error,
            # a timeout, or an empty body must all leave the caller with the
            # summary it already had.
            _log.warning("Thread summarization failed: %s", error)
            return previous
