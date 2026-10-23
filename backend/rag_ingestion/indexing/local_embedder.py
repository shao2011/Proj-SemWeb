from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from rag_ingestion.indexing.qdrant_index import EmbeddingProfile


_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[-_.:/][^\W_]+)*", re.UNICODE)


class LocalHashEmbedder:
    def __init__(self, dimensions: int = 384) -> None:
        if dimensions < 1:
            raise ValueError("Embedding dimensions must be positive")
        self.dimensions = dimensions
        self.profile = EmbeddingProfile(
            provider="local",
            model="feature-hash",
            version="1",
            dimensions=dimensions,
            task="retrieval",
            mode="naive",
        )

    def embed_documents(
        self,
        items: list[str],
        profile: EmbeddingProfile,
        *,
        intent: Literal["document"],
    ) -> list[list[float]]:
        self._validate_profile(profile)
        return [self._embed(item) for item in items]

    def embed_query(
        self,
        text: str,
        profile: EmbeddingProfile,
        *,
        intent: Literal["query"],
    ) -> list[float]:
        self._validate_profile(profile)
        return self._embed(text)

    def _validate_profile(self, profile: EmbeddingProfile) -> None:
        if profile != self.profile:
            raise ValueError("Embedding profile does not match LocalHashEmbedder")

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN_PATTERN.findall(text.casefold()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector
