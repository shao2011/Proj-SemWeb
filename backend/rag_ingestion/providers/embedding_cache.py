from __future__ import annotations

import hashlib
import json
import math
from typing import Literal, Protocol

from redis.exceptions import RedisError

from rag_ingestion.indexing.qdrant_index import Embedder, EmbeddingProfile
from rag_ingestion.logger import get_logger


_log = get_logger(__name__)
_KEY_PREFIX = "embedding:query:v1"


class RedisClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...

    def setex(self, key: str, ttl: int, value: str) -> bool | None: ...


class RedisCachedEmbedder:
    def __init__(
        self,
        delegate: Embedder,
        redis: RedisClient,
        *,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("Embedding cache TTL must be positive")
        self._delegate = delegate
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def cache_key(text: str, profile: EmbeddingProfile) -> str:
        identity = profile.model_dump_json()
        digest = hashlib.sha256(f"{identity}\0{text}".encode()).hexdigest()
        return f"{_KEY_PREFIX}:{digest}"

    @staticmethod
    def _decode(raw: bytes | str | None, dimensions: int) -> list[float] | None:
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, list) or len(payload) != dimensions:
                return None
            vector = [float(value) for value in payload]
        except (TypeError, ValueError):
            return None
        return vector if all(math.isfinite(value) for value in vector) else None

    def embed_query(
        self,
        text: str,
        profile: EmbeddingProfile,
        *,
        intent: Literal["query"],
    ) -> list[float]:
        key = self.cache_key(text, profile)
        try:
            cached = self._decode(self._redis.get(key), profile.dimensions)
        except RedisError as error:
            _log.warning("Redis embedding cache read failed: %s", error)
            cached = None
        if cached is not None:
            return cached

        vector = self._delegate.embed_query(text, profile, intent=intent)
        if len(vector) != profile.dimensions or not all(
            math.isfinite(value) for value in vector
        ):
            # Passed through rather than raised: rejecting a malformed provider
            # response belongs at the index boundary, which does raise, and doing
            # it twice would make the same fault surface differently depending on
            # whether a cache happened to be configured. Logged because the
            # alternative is a decision no one can see: the vector is declined for
            # the cache and nothing says why.
            _log.warning(
                "Refusing to cache a malformed embedding for profile %s: "
                "%d dimension(s), finite=%s",
                profile.model,
                len(vector),
                all(math.isfinite(value) for value in vector),
            )
            return vector
        try:
            self._redis.setex(
                key,
                self._ttl_seconds,
                json.dumps(vector, separators=(",", ":")),
            )
        except RedisError as error:
            _log.warning("Redis embedding cache write failed: %s", error)
        return vector

    def embed_documents(
        self,
        items: list[str],
        profile: EmbeddingProfile,
        *,
        intent: Literal["document"],
    ) -> list[list[float]]:
        return self._delegate.embed_documents(items, profile, intent=intent)