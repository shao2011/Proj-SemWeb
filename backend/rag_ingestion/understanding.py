"""UnderstandMessage: query rewrite and memory extraction, one LLM call.

Runs on every user message, project thread or plain chat. `rewritten_query`
feeds retrieval only; the original message is always what reaches the
answerer. Any failure (provider error, malformed JSON, a candidate that fails
validation) degrades to `Understanding(None, [])` rather than breaking the
turn -- extraction is a bonus, never a dependency for answering.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.logger import get_logger
from rag_ingestion.prompts.understanding import UNDERSTAND_SYSTEM_PROMPT
from rag_ingestion.schemas.understanding import MemoryCandidate, Understanding
from rag_ingestion.memory.models import is_valid_key

_log = get_logger(__name__)

_MEMORY_CAP = 3


@runtime_checkable
class Understander(Protocol):
    def understand(
        self,
        query: str,
        context: str,
        *,
        sticky_key: str | None = None,
        #: The session's picked model: understanding runs on whatever the user
        #: is chatting with, so its quota and its quirks stay one model's.
        model: str | None = None,
        router: str | None = None,
    ) -> Understanding: ...


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped.removeprefix("```json").removeprefix("```")
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _parse(raw_text: str) -> Understanding:
    payload = json.loads(_strip_code_fence(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("Understanding response must be a JSON object")

    rewritten_query = payload.get("rewritten_query")
    if rewritten_query is not None and not isinstance(rewritten_query, str):
        raise ValueError("rewritten_query must be a string or null")

    raw_memory = payload.get("memory") or []
    if not isinstance(raw_memory, list):
        raise ValueError("memory must be a list or null")

    candidates: list[MemoryCandidate] = []
    for item in raw_memory:
        if len(candidates) >= _MEMORY_CAP:
            break
        try:
            candidate = MemoryCandidate.model_validate(item)
        except ValidationError:
            # One bad candidate (an unknown type, or a field the model
            # invented) does not invalidate the rewrite or the other
            # candidates in the same response.
            continue
        if not is_valid_key(candidate.key):
            continue
        candidates.append(candidate)

    return Understanding(rewritten_query=rewritten_query, memory=candidates)


class LLMUnderstander:
    def __init__(self, chat: ChatCompleter, *, max_tokens: int = 256) -> None:
        self._chat = chat
        self._max_tokens = max_tokens

    def understand(
        self,
        query: str,
        context: str,
        *,
        sticky_key: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> Understanding:
        user_content = f"{context}\n\nCurrent message:\n{query}" if context else f"Current message:\n{query}"
        turns = [
            ChatTurn(role="system", content=UNDERSTAND_SYSTEM_PROMPT),
            ChatTurn(role="user", content=user_content),
        ]
        try:
            raw = self._chat.complete(
                turns,
                max_tokens=self._max_tokens,
                sticky_key=sticky_key,
                model=model,
                router=router,
            )
            return _parse(raw)
        except Exception as error:
            # Deliberately broad: a provider failure, a timeout, malformed
            # JSON, or an unexpected shape must all fall back the same way.
            # This node is a bonus signal, never a dependency of the answer
            # path, so nothing it can throw is allowed to reach the caller.
            _log.warning("UnderstandMessage failed for query=%r: %s", query, error)
            return Understanding(rewritten_query=None, memory=[])
