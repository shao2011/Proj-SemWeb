"""Jina embedding helpers: the system's provider bundle, late chunking (C4),
precomputed vectors, and a LangChain wrapper for the memory libraries."""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Sequence

import httpx
from langchain_core.embeddings import Embeddings

from rag_ingestion.config import RuntimeConfig
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.providers.registry import Provider, ProviderBundle
from scripts.benchmark.mtrag_run import _bundle

from eval.settings import LATE_CHUNKING_WINDOW_TOKENS

JINA_EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"


def jina_bundle(config: RuntimeConfig | None = None) -> ProviderBundle:
    """The system's Jina embedder and reranker, built without the web app."""
    return _bundle(config or RuntimeConfig.from_environment(), Provider.JINA)


def estimate_tokens(text: str) -> int:
    """Four characters a token, the estimate the MTRAG runner paces with."""
    return len(text) // 4 + 1


def pack_groups(groups: Sequence[Sequence[str]], max_tokens: int = LATE_CHUNKING_WINDOW_TOKENS) -> list[list[str]]:
    """Pack whole sections into requests that fit the model window.

    A section is never split across requests unless it alone is too long; then
    it is cut at chunk boundaries (C4).
    """
    requests: list[list[str]] = []
    current: list[str] = []
    spent = 0
    for group in groups:
        size = sum(estimate_tokens(text) for text in group)
        if size > max_tokens:
            if current:
                requests.append(current)
                current, spent = [], 0
            piece: list[str] = []
            piece_size = 0
            for text in group:
                cost = estimate_tokens(text)
                if piece and piece_size + cost > max_tokens:
                    requests.append(piece)
                    piece, piece_size = [], 0
                piece.append(text)
                piece_size += cost
            if piece:
                requests.append(piece)
            continue
        if current and spent + size > max_tokens:
            requests.append(current)
            current, spent = [], 0
        current.extend(group)
        spent += size
    if current:
        requests.append(current)
    return requests


class LateChunkingClient:
    """Jina's `late_chunking` flag: one request encodes its inputs as one text
    and returns a vector per input pooled from that shared encoding."""

    def __init__(
        self,
        api_keys: list[str],
        profile: EmbeddingProfile,
        *,
        post: Callable[..., httpx.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_keys:
            raise SystemExit("No Jina API key (JINA_API_KEY)")
        self._keys = itertools.cycle(api_keys)
        self._profile = profile
        self._post = post or httpx.post
        self._sleep = sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self._profile.model,
            "task": "retrieval.passage",
            "dimensions": self._profile.dimensions,
            "late_chunking": True,
            "input": texts,
        }
        for attempt in range(1, 6):
            response = self._post(
                JINA_EMBEDDINGS_URL,
                json=payload,
                headers={"Authorization": f"Bearer {next(self._keys)}"},
                timeout=120.0,
            )
            if response.status_code == 200:
                data = sorted(response.json()["data"], key=lambda item: item["index"])
                return [item["embedding"] for item in data]
            if attempt == 5:
                response.raise_for_status()
            self._sleep(min(2**attempt, 60))
        raise AssertionError("unreachable")


class PrecomputedEmbedder:
    """Serves stored vectors for known texts; anything else goes to `delegate`."""

    def __init__(self, vectors: dict[str, list[float]], delegate) -> None:
        self._vectors = vectors
        self._delegate = delegate

    def embed_documents(self, items, profile, *, intent):
        missing = [text for text in items if text not in self._vectors]
        if missing:
            for text, vector in zip(missing, self._delegate.embed_documents(missing, profile, intent=intent)):
                self._vectors[text] = vector
        return [self._vectors[text] for text in items]

    def embed_query(self, text, profile, *, intent):
        return self._delegate.embed_query(text, profile, intent=intent)


class LangChainJina(Embeddings):
    """The system's Jina embedder behind LangChain's interface (Mem0, LangMem)."""

    def __init__(self, bundle: ProviderBundle) -> None:
        self._embedder = bundle.embedder
        self._profile = bundle.profile

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embedder.embed_documents(list(texts), self._profile, intent="document")

    def embed_query(self, text: str) -> list[float]:
        return self._embedder.embed_query(text, self._profile, intent="query")
