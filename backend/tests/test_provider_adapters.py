import json

import httpx
import pytest

from rag_ingestion.retrieval.answering import AnswerEvidence, MockAnswerer
from rag_ingestion.providers.adapters import (
    CohereEmbedder,
    CohereReranker,
    JinaEmbedder,
    JinaReranker,
    VoyageEmbedder,
    VoyageReranker,
)
from rag_ingestion.providers.registry import (
    Provider,
    ProviderBundle,
    ProviderRegistry,
    ProviderRequestError,
    ProviderUnavailableError,
)
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile


def _profile(provider: Provider, model: str) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider=provider.value,
        model=model,
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )


@pytest.mark.parametrize(
    (
        "adapter_type",
        "provider",
        "model",
        "path",
        "intent_field",
        "query_intent",
        "document_intent",
        "response",
    ),
    [
        (
            CohereEmbedder,
            Provider.COHERE,
            "embed-v4.0",
            "/v2/embed",
            "input_type",
            "search_query",
            "search_document",
            {"embeddings": {"float": [[1.0, 0.0], [0.0, 1.0]]}},
        ),
        (
            VoyageEmbedder,
            Provider.VOYAGE,
            "voyage-4",
            "/v1/embeddings",
            "input_type",
            "query",
            "document",
            {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]},
        ),
        (
            JinaEmbedder,
            Provider.JINA,
            "jina-embeddings-v4",
            "/v1/embeddings",
            "task",
            "retrieval.query",
            "retrieval.passage",
            {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]},
        ),
    ],
)
def test_embedding_adapters_map_native_intents_and_responses(
    adapter_type,
    provider,
    model,
    path,
    intent_field,
    query_intent,
    document_intent,
    response,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        count = len(payload["texts"] if provider is Provider.COHERE else payload["input"])
        if count == 1:
            if provider is Provider.COHERE:
                body = {"embeddings": {"float": [[1.0, 0.0]]}}
            else:
                body = {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=response)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://example.test",
    )
    profile = _profile(provider, model)
    adapter = adapter_type(["secret-key"], profile, client=client)

    documents = adapter.embed_documents(["one", "two"], profile, intent="document")
    query = adapter.embed_query("one", profile, intent="query")

    assert documents == [[1.0, 0.0], [0.0, 1.0]]
    assert query == [1.0, 0.0]
    assert [request.url.path for request in requests] == [path, path]
    assert [json.loads(request.content)[intent_field] for request in requests] == [
        document_intent,
        query_intent,
    ]
    assert all(request.headers["authorization"] == "Bearer secret-key" for request in requests)


@pytest.mark.parametrize(
    ("adapter_type", "model", "path", "limit_field", "extra", "response_body"),
    [
        (
            CohereReranker,
            "rerank-v4.0-pro",
            "/v2/rerank",
            "top_n",
            {},
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
        ),
        (
            VoyageReranker,
            "rerank-2.5",
            "/v1/rerank",
            "top_k",
            {"return_documents": False, "truncation": False},
            # Voyage puts ranked results under "data", not "results".
            {
                "object": "list",
                "data": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ],
                "model": "rerank-2.5",
                "usage": {"total_tokens": 12},
            },
        ),
        (
            JinaReranker,
            "jina-reranker-v3",
            "/v1/rerank",
            "top_n",
            {},
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
        ),
    ],
)
def test_rerank_adapters_map_native_payload_and_order(
    adapter_type, model, path, limit_field, extra, response_body
) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response_body)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    adapter = adapter_type(["secret-key"], model, client=client)

    ranked = adapter.rerank("question", ["first", "second"], top_n=2)

    assert [(item.index, item.relevance_score) for item in ranked] == [(1, 0.9), (0, 0.4)]
    assert captured == [
        {
            "model": model,
            "query": "question",
            "documents": ["first", "second"],
            limit_field: 2,
            **extra,
        }
    ]
    assert client.base_url.join(path).path == path


def test_registry_tolerates_missing_credentials_and_sanitizes_provider_errors() -> None:
    registry = ProviderRegistry()

    with pytest.raises(ProviderUnavailableError, match="cohere") as unavailable:
        registry.get(Provider.COHERE)
    assert "key" not in str(unavailable.value).casefold()

    profile = _profile(Provider.COHERE, "embed-v4.0")
    bundle = ProviderBundle(
        provider=Provider.COHERE,
        profile=profile,
        collection_name="rag-cohere-v1",
        embedder=object(),
        reranker=object(),
    )
    registry.register(bundle)
    assert registry.get(Provider.COHERE) is bundle

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret-key leaked by vendor")

    adapter = CohereEmbedder(
        ["secret-key"],
        profile,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.test"),
    )
    with pytest.raises(ProviderRequestError) as failed:
        adapter.embed_query("query", profile, intent="query")
    assert "secret-key" not in str(failed.value)
    assert "leaked by vendor" not in str(failed.value)


def test_embedder_round_robins_across_multiple_keys() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"embeddings": {"float": [[1.0, 0.0]]}})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    profile = _profile(Provider.COHERE, "embed-v4.0")
    adapter = CohereEmbedder(["key-a", "key-b"], profile, client=client)

    for _ in range(4):
        adapter.embed_query("q", profile, intent="query")

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer key-a",
        "Bearer key-b",
        "Bearer key-a",
        "Bearer key-b",
    ]


def test_reranker_round_robins_across_multiple_keys() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://example.test"
    )
    adapter = CohereReranker(["key-a", "key-b"], "rerank-v4.0-pro", client=client)

    for _ in range(4):
        adapter.rerank("q", ["doc"], top_n=1)

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer key-a",
        "Bearer key-b",
        "Bearer key-a",
        "Bearer key-b",
    ]


def test_embedder_rejects_an_empty_key_list() -> None:
    profile = _profile(Provider.COHERE, "embed-v4.0")
    with pytest.raises(ValueError):
        CohereEmbedder([], profile)


def test_mock_answer_is_deterministic_and_uses_only_supplied_evidence() -> None:
    answerer = MockAnswerer()
    evidence = [
        AnswerEvidence(chunk_id="chunk-b", source_text="Second"),
        AnswerEvidence(chunk_id="chunk-a", source_text="First"),
    ]

    first = answerer.answer("What changed?", evidence)
    second = answerer.answer("What changed?", evidence)
    empty = answerer.answer("What changed?", [])

    assert first == second
    assert first.citations == ["chunk-b", "chunk-a"]
    assert "Second" in first.text
    assert empty.citations == []
    assert "no evidence" in empty.text.casefold()
