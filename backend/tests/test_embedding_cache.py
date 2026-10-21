import json

import pytest
from redis.exceptions import RedisError

from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.providers.embedding_cache import RedisCachedEmbedder


_PROFILE = EmbeddingProfile(
    provider="cohere",
    model="embed-v4.0",
    version="v1",
    dimensions=2,
    task="retrieval",
    mode="naive",
)


class RecordingEmbedder:
    def __init__(self) -> None:
        self.query_calls: list[str] = []
        self.document_calls: list[list[str]] = []

    def embed_query(self, text, profile, *, intent):
        self.query_calls.append(text)
        return [1.0, 2.0]

    def embed_documents(self, items, profile, *, intent):
        self.document_calls.append(items)
        return [[1.0, 2.0] for _ in items]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, int, str]] = []

    def get(self, key: str):
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.set_calls.append((key, ttl, value))
        self.values[key] = value.encode()


class FailingRedis(FakeRedis):
    def __init__(self, *, fail_get: bool = False, fail_set: bool = False) -> None:
        super().__init__()
        self.fail_get = fail_get
        self.fail_set = fail_set

    def get(self, key: str):
        if self.fail_get:
            raise RedisError("unavailable")
        return super().get(key)

    def setex(self, key: str, ttl: int, value: str):
        if self.fail_set:
            raise RedisError("unavailable")
        super().setex(key, ttl, value)


def test_repeated_query_uses_cached_vector_without_exposing_query_in_key() -> None:
    delegate = RecordingEmbedder()
    redis = FakeRedis()
    embedder = RedisCachedEmbedder(delegate, redis, ttl_seconds=60)

    first = embedder.embed_query("private question", _PROFILE, intent="query")
    second = embedder.embed_query("private question", _PROFILE, intent="query")

    assert first == second == [1.0, 2.0]
    assert delegate.query_calls == ["private question"]
    assert len(redis.set_calls) == 1
    key, ttl, payload = redis.set_calls[0]
    assert key.startswith("embedding:query:v1:")
    assert "private question" not in key
    assert ttl == 60
    assert json.loads(payload) == [1.0, 2.0]


def test_document_embeddings_always_bypass_redis() -> None:
    delegate = RecordingEmbedder()
    redis = FakeRedis()
    embedder = RedisCachedEmbedder(delegate, redis, ttl_seconds=60)

    first = embedder.embed_documents(["one"], _PROFILE, intent="document")
    second = embedder.embed_documents(["one"], _PROFILE, intent="document")

    assert first == second == [[1.0, 2.0]]
    assert delegate.document_calls == [["one"], ["one"]]
    assert redis.values == {}


@pytest.mark.parametrize("cached", [b"[1.0]", b"not-json", b"[NaN, 2.0]"])
def test_invalid_cached_vector_is_replaced(cached: bytes) -> None:
    delegate = RecordingEmbedder()
    redis = FakeRedis()
    embedder = RedisCachedEmbedder(delegate, redis, ttl_seconds=60)
    key = embedder.cache_key("question", _PROFILE)
    redis.values[key] = cached

    vector = embedder.embed_query("question", _PROFILE, intent="query")

    assert vector == [1.0, 2.0]
    assert delegate.query_calls == ["question"]
    assert json.loads(redis.values[key]) == [1.0, 2.0]


def test_redis_read_or_write_failure_falls_back_to_provider() -> None:
    read_delegate = RecordingEmbedder()
    read_embedder = RedisCachedEmbedder(
        read_delegate, FailingRedis(fail_get=True), ttl_seconds=60
    )
    write_delegate = RecordingEmbedder()
    write_embedder = RedisCachedEmbedder(
        write_delegate, FailingRedis(fail_set=True), ttl_seconds=60
    )

    assert read_embedder.embed_query("read", _PROFILE, intent="query") == [1.0, 2.0]
    assert write_embedder.embed_query("write", _PROFILE, intent="query") == [1.0, 2.0]
    assert read_delegate.query_calls == ["read"]
    assert write_delegate.query_calls == ["write"]