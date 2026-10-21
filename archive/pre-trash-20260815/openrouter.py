from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx

from rag_ingestion.chat import ChatTurn
from rag_ingestion.logger import get_logger
from rag_ingestion.providers.keys import StickyKeyPool
from rag_ingestion.providers.registry import (
    ProviderRequestError,
    ProviderResponseError,
)


_DONE = "[DONE]"
_DATA_PREFIX = "data:"

_log = get_logger(__name__)


class OpenRouterChat:
    """Chat completions through OpenRouter's OpenAI-compatible endpoint.

    Provider text is never forwarded into raised errors: an upstream body can
    echo the request, and the request carries the API key.
    """

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://openrouter.ai",
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._keys = StickyKeyPool(api_keys)

    def _headers(self, sticky_key: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._keys.key_for(sticky_key)}"}

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: Callable[[dict[str, object]], None] | None = None,
        model: str | None = None,
    ) -> str:
        try:
            response = self._client.post(
                "/api/v1/chat/completions",
                json=self._payload(turns, max_tokens, stream=False, model=model),
                headers=self._headers(sticky_key),
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderRequestError("Chat provider request failed") from error

        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ProviderResponseError(
                "Chat provider returned an invalid response"
            ) from error
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError("Chat provider returned empty text")
        usage = body.get("usage")
        if isinstance(usage, dict):
            _log.info("OpenRouter usage for model=%s: %s", self.model, usage)
            if on_usage is not None:
                on_usage(usage)
        return text

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage: Callable[[dict[str, object]], None] | None = None,
        model: str | None = None,
    ) -> Iterator[str]:
        try:
            with self._client.stream(
                "POST",
                "/api/v1/chat/completions",
                json=self._payload(turns, max_tokens, stream=True, model=model),
                headers=self._headers(sticky_key),
            ) as response:
                if response.is_error:
                    response.read()
                    raise ProviderRequestError("Chat provider request failed")
                for line in response.iter_lines():
                    for text in self._deltas(line):
                        yield text
                    usage = self._usage(line)
                    if usage is not None:
                        _log.info(
                            "OpenRouter usage for model=%s: %s", self.model, usage
                        )
                        if on_usage is not None:
                            on_usage(usage)
                    if self._is_done(line):
                        return
        except httpx.HTTPError as error:
            raise ProviderRequestError("Chat provider request failed") from error

    def _payload(
        self,
        turns: list[ChatTurn],
        max_tokens: int,
        *,
        stream: bool,
        model: str | None = None,
    ) -> dict[str, object]:
        return {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "stream": stream,
            "messages": [{"role": turn.role, "content": turn.content} for turn in turns],
        }

    @staticmethod
    def _is_done(line: str) -> bool:
        stripped = line.strip()
        return (
            stripped.startswith(_DATA_PREFIX)
            and stripped[len(_DATA_PREFIX) :].strip() == _DONE
        )

    @staticmethod
    def _deltas(line: str) -> list[str]:
        stripped = line.strip()
        if not stripped.startswith(_DATA_PREFIX):
            return []
        data = stripped[len(_DATA_PREFIX) :].strip()
        if not data or data == _DONE:
            return []
        try:
            chunk = json.loads(data)
            choices = chunk["choices"]
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderResponseError(
                "Chat provider returned an invalid stream chunk"
            ) from error
        texts: list[str] = []
        for choice in choices:
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str) and content:
                texts.append(content)
        return texts

    @staticmethod
    def _usage(line: str) -> dict[str, object] | None:
        """The final SSE chunk carries token/cache usage alongside `choices: []`."""
        stripped = line.strip()
        if not stripped.startswith(_DATA_PREFIX):
            return None
        data = stripped[len(_DATA_PREFIX) :].strip()
        if not data or data == _DONE:
            return None
        try:
            chunk = json.loads(data)
        except ValueError:
            return None
        usage = chunk.get("usage")
        return usage if isinstance(usage, dict) else None
