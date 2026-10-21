from __future__ import annotations

from typing import Literal

import httpx

from rag_ingestion.providers.registry import ProviderRequestError, ProviderResponseError
from rag_ingestion.providers.keys import KeyRotator
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.retrieval.reranking import RankedResult


def _post(
    client: httpx.Client,
    path: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        response = client.post(path, json=payload, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ProviderRequestError("Provider request failed") from error
    try:
        body = response.json()
    except ValueError as error:
        raise ProviderResponseError("Provider returned an invalid response") from error
    if not isinstance(body, dict):
        raise ProviderResponseError("Provider returned an invalid response")
    return body


def _client(base_url: str, timeout: float) -> httpx.Client:
    # No default Authorization header: with multiple keys in rotation, auth
    # is a per-request header (see `_EmbedderBase._headers`), not a fixed
    # client default that every request would otherwise share.
    return httpx.Client(base_url=base_url, timeout=timeout)


def _indexed_embeddings(body: dict[str, object]) -> list[list[float]]:
    try:
        data = body["data"]
        if not isinstance(data, list):
            raise TypeError
        ordered = sorted(data, key=lambda item: item["index"])
        return [[float(value) for value in item["embedding"]] for item in ordered]
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderResponseError("Provider returned invalid embeddings") from error


def _ranked_results(
    body: dict[str, object], *, field: str = "results"
) -> list[RankedResult]:
    try:
        results = body[field]
        if not isinstance(results, list):
            raise TypeError
        return [
            RankedResult(
                index=item["index"],
                relevance_score=item["relevance_score"],
            )
            for item in results
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderResponseError("Provider returned invalid rerank results") from error


class _EmbedderBase:
    def __init__(
        self,
        api_keys: list[str],
        profile: EmbeddingProfile,
        *,
        client: httpx.Client,
    ) -> None:
        self.profile = profile
        self._client = client
        self._rotator = KeyRotator(api_keys)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._rotator.next_key()}"}

    def _validate_profile(self, profile: EmbeddingProfile) -> None:
        if profile != self.profile:
            raise ValueError("Embedding profile does not match adapter configuration")


class CohereEmbedder(_EmbedderBase):
    def __init__(
        self,
        api_keys: list[str],
        profile: EmbeddingProfile,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.cohere.com",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            api_keys, profile, client=client or _client(base_url, timeout)
        )

    def _embed(self, texts: list[str], profile: EmbeddingProfile, input_type: str) -> list[list[float]]:
        self._validate_profile(profile)
        body = _post(
            self._client,
            "/v2/embed",
            {
                "model": profile.model,
                "texts": texts,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": profile.dimensions,
            },
            headers=self._headers(),
        )
        try:
            vectors = body["embeddings"]["float"]
            return [[float(value) for value in vector] for vector in vectors]
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderResponseError("Provider returned invalid embeddings") from error

    def embed_documents(self, items: list[str], profile: EmbeddingProfile, *, intent: Literal["document"]) -> list[list[float]]:
        return self._embed(items, profile, "search_document")

    def embed_query(self, text: str, profile: EmbeddingProfile, *, intent: Literal["query"]) -> list[float]:
        return self._embed([text], profile, "search_query")[0]


class VoyageEmbedder(_EmbedderBase):
    def __init__(
        self,
        api_keys: list[str],
        profile: EmbeddingProfile,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.voyageai.com",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            api_keys, profile, client=client or _client(base_url, timeout)
        )

    def _embed(self, texts: list[str], profile: EmbeddingProfile, input_type: str) -> list[list[float]]:
        self._validate_profile(profile)
        return _indexed_embeddings(
            _post(
                self._client,
                "/v1/embeddings",
                {
                    "model": profile.model,
                    "input": texts,
                    "input_type": input_type,
                    "output_dimension": profile.dimensions,
                },
                headers=self._headers(),
            )
        )

    def embed_documents(self, items: list[str], profile: EmbeddingProfile, *, intent: Literal["document"]) -> list[list[float]]:
        return self._embed(items, profile, "document")

    def embed_query(self, text: str, profile: EmbeddingProfile, *, intent: Literal["query"]) -> list[float]:
        return self._embed([text], profile, "query")[0]


class JinaEmbedder(_EmbedderBase):
    def __init__(
        self,
        api_keys: list[str],
        profile: EmbeddingProfile,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.jina.ai",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            api_keys, profile, client=client or _client(base_url, timeout)
        )

    def _embed(self, texts: list[str], profile: EmbeddingProfile, task: str) -> list[list[float]]:
        self._validate_profile(profile)
        return _indexed_embeddings(
            _post(
                self._client,
                "/v1/embeddings",
                {
                    "model": profile.model,
                    "input": texts,
                    "task": task,
                    "dimensions": profile.dimensions,
                },
                headers=self._headers(),
            )
        )

    def embed_documents(self, items: list[str], profile: EmbeddingProfile, *, intent: Literal["document"]) -> list[list[float]]:
        return self._embed(items, profile, "retrieval.passage")

    def embed_query(self, text: str, profile: EmbeddingProfile, *, intent: Literal["query"]) -> list[float]:
        return self._embed([text], profile, "retrieval.query")[0]


class _RerankerBase:
    path: str
    limit_field: str
    results_field: str = "results"

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        client: httpx.Client,
    ) -> None:
        self.model = model
        self._client = client
        self._rotator = KeyRotator(api_keys)

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[RankedResult]:
        payload: dict[str, object] = {
            "model": self.model,
            "query": query,
            "documents": documents,
            self.limit_field: top_n,
        }
        payload.update(self._extra_payload())
        headers = {"Authorization": f"Bearer {self._rotator.next_key()}"}
        return _ranked_results(
            _post(self._client, self.path, payload, headers=headers),
            field=self.results_field,
        )

    def _extra_payload(self) -> dict[str, object]:
        return {}


class CohereReranker(_RerankerBase):
    path = "/v2/rerank"
    limit_field = "top_n"

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.cohere.com",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(api_keys, model, client=client or _client(base_url, timeout))


class VoyageReranker(_RerankerBase):
    path = "/v1/rerank"
    limit_field = "top_k"
    # Voyage returns ranked results under "data"; Cohere and Jina use "results".
    results_field = "data"

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.voyageai.com",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(api_keys, model, client=client or _client(base_url, timeout))

    def _extra_payload(self) -> dict[str, object]:
        return {"return_documents": False, "truncation": False}


class JinaReranker(_RerankerBase):
    path = "/v1/rerank"
    limit_field = "top_n"

    def __init__(
        self,
        api_keys: list[str],
        model: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://api.jina.ai",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(api_keys, model, client=client or _client(base_url, timeout))
