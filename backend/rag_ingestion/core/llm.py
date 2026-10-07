"""Where a chat model object comes from.

One function, so swapping providers is a configuration change rather than an
edit to the agent, the routes, or anything that calls them. `init_chat_model`
is LangChain's provider registry: the same call returns an Anthropic, Google,
Ollama or OpenAI-compatible client depending on `provider`, and every one of
them satisfies the interface `create_agent` expects.

The model is chosen per request -- users pick one, and the bookkeeping nodes
run cheaper models than generation -- so this cannot be a singleton built at
startup. It is cached instead, keyed by everything that shapes the client,
because rebuilding one per turn also rebuilds its connection pool.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from rag_ingestion.config import DEFAULT_LLM_PROVIDER

# The provider is passed explicitly rather than inferred: OpenRouter model ids
# contain a slash ("google/gemini-2.5-flash"), which is also
# `init_chat_model`'s provider separator.
__all__ = ["DEFAULT_LLM_PROVIDER", "build_chat_model"]


@lru_cache(maxsize=64)
def _cached_model(
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None,
    timeout: float,
    max_tokens: int | None,
) -> Any:
    from langchain.chat_models import init_chat_model

    options: dict[str, Any] = {
        "model_provider": provider,
        "api_key": api_key,
        "timeout": timeout,
    }
    if base_url:
        options["base_url"] = base_url
    if max_tokens:
        options["max_completion_tokens"] = max_tokens
    if provider == "openai":
        # Without this OpenRouter omits usage from a streamed response and the
        # accounting table silently stops filling. Not every provider accepts
        # the flag, so it is set only where it is known to apply.
        options["stream_usage"] = True
    return init_chat_model(model, **options)


def build_chat_model(
    *,
    model: str,
    api_key: str,
    base_url: str | None = None,
    provider: str = DEFAULT_LLM_PROVIDER,
    timeout: float = 120.0,
    max_tokens: int | None = None,
) -> Any:
    """A chat model ready for `create_agent`.

    Cached per distinct client, so a thread that keeps its API key (sticky
    rotation) and its model keeps its connection pool too.
    """
    return _cached_model(provider, model, api_key, base_url, timeout, max_tokens)
