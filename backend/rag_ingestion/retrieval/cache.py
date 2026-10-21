"""Cache for a whole `RetrievalOutcome`, keyed by everything that shapes it.

A query costs one embedding call, three or four Qdrant searches, and one
rerank over the whole candidate pool. The reranker charges per document it
scores, so that last step dominates: caching the finished outcome is the only
thing that skips it entirely.

Two rules make this safe rather than merely fast.

**The key carries every input.** Query text, rewrite, tenant, retrieval depths
and embedding profile all change the result, so all of them are in the key. A
key that omits one serves an answer computed for a different question.

**A new document invalidates the project.** Each project carries a version
counter that a completed upload bumps; the counter is part of the key, so
every cached entry for that project is unreachable from the next request on.
Without it a user who asks the same question after uploading would never see
the document they just added, and would get a confident citation to the old
evidence instead -- worse than a slow answer.

The cache is best-effort throughout: every Redis failure and every unreadable
entry degrades to a miss, because a retrieval that is slow still answers and
one that raises does not.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from rag_ingestion.chunking import Chunk
from rag_ingestion.config import RetrievalConfig
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.logger import get_logger
from rag_ingestion.retrieval.chain import RetrievalOutcome

_log = get_logger(__name__)

# v2: the key gained the reranker model. v1 entries hashed without it decode
# fine but answer for the wrong reranker, so they are stranded by prefix and
# age out on their TTL rather than being migrated.
# v3: the key gained the excluded-documents list (trash + per-question
# unchecked docs); same stranding rule.
_ENTRY_PREFIX = "retrieval:outcome:v3"
_VERSION_PREFIX = "retrieval:index_version:v1"


class RedisClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...

    def setex(self, key: str, ttl: int, value: str) -> bool | None: ...

    def incr(self, key: str) -> int: ...


class RetrievalCache:
    def __init__(self, redis: RedisClient, *, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            raise ValueError("Retrieval cache TTL must be positive")
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    # --- invalidation -------------------------------------------------------

    @staticmethod
    def _version_key(project_id: str) -> str:
        return f"{_VERSION_PREFIX}:{project_id}"

    def _index_version(self, project_id: str) -> str:
        """Current version, or `0` when Redis cannot say.

        A failure here is not fatal: it only means the key is built from a
        stale version, and the entry it points at expires on its own TTL.
        """
        try:
            raw = self._redis.get(self._version_key(project_id))
        except Exception as error:
            _log.warning("Retrieval cache version read failed: %s", error)
            return "0"
        if raw is None:
            return "0"
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    def invalidate_project(self, project_id: str) -> None:
        """Bump the version so every existing entry for the project is orphaned.

        Bumping a counter rather than scanning and deleting keys: `SCAN` over a
        large keyspace to find one project's entries is slow and runs on the
        request that just finished an upload. The orphans age out under their
        own TTL and the LRU policy.
        """
        try:
            version = self._redis.incr(self._version_key(project_id))
        except Exception as error:
            # Losing the bump would serve stale results, so it is reported at
            # WARNING rather than swallowed silently.
            _log.warning(
                "Retrieval cache invalidation failed for project=%s: %s",
                project_id,
                error,
            )
            return
        _log.info(
            "Retrieval cache invalidated for project=%s (index version %s)",
            project_id,
            version,
        )

    # --- key ----------------------------------------------------------------

    def _key(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        query: str,
        rewritten_query: str | None,
        profile: EmbeddingProfile,
        config: RetrievalConfig,
        limit: int,
        language: str | None,
        reranker: str,
        excluded_doc_ids: list[str] | None = None,
    ) -> str:
        identity = json.dumps(
            [
                owner_user_id,
                project_id,
                query,
                rewritten_query,
                # Which documents were excluded (trash + per-question
                # unchecked) changes the outcome as much as the query does.
                sorted(excluded_doc_ids or []),
                profile.model_dump(mode="json"),
                # The reranker decides which chunks the outcome holds and what
                # they scored, so it is as much an input as the embedding
                # profile. It was the one input missing from the key: changing
                # the rerank model kept serving rankings the old model computed
                # for a TTL, and two instances configured differently would read
                # each other's results off a shared Redis.
                reranker,
                config.model_dump(mode="json"),
                limit,
                language,
                self._index_version(project_id),
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{_ENTRY_PREFIX}:{hashlib.sha256(identity.encode()).hexdigest()}"

    # --- read / write -------------------------------------------------------

    def get(self, **key_parts) -> RetrievalOutcome | None:
        project_id = key_parts.get("project_id")
        try:
            raw = self._redis.get(self._key(**key_parts))
        except Exception as error:
            _log.warning("Retrieval cache read failed: %s", error)
            return None
        if raw is None:
            _log.info("Retrieval cache miss for project=%s", project_id)
            return None
        try:
            outcome = _decode(raw)
        except Exception as error:
            # A payload written by an older shape, or a truncated value. Not
            # worth failing a request over.
            _log.warning("Retrieval cache entry unreadable, treating as miss: %s", error)
            return None
        _log.info("Retrieval cache hit for project=%s", project_id)
        return outcome

    def set(self, outcome: RetrievalOutcome, **key_parts) -> None:
        try:
            self._redis.setex(
                self._key(**key_parts), self._ttl_seconds, _encode(outcome)
            )
        except Exception as error:
            _log.warning("Retrieval cache write failed: %s", error)


def _encode(outcome: RetrievalOutcome) -> str:
    return json.dumps(
        {
            "chunks": [chunk.model_dump(mode="json") for chunk in outcome.chunks],
            "context_chunks": [
                chunk.model_dump(mode="json") for chunk in outcome.context_chunks
            ],
            "evidence_leaf_ids": outcome.evidence_leaf_ids,
            "scores": outcome.scores,
            "routed_doc_ids": outcome.routed_doc_ids,
            "diagnostics": outcome.diagnostics,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode(raw: bytes | str) -> RetrievalOutcome:
    payload = json.loads(raw)
    return RetrievalOutcome(
        chunks=[Chunk.model_validate(item) for item in payload["chunks"]],
        context_chunks=[
            Chunk.model_validate(item) for item in payload["context_chunks"]
        ],
        evidence_leaf_ids=list(payload["evidence_leaf_ids"]),
        scores=dict(payload["scores"]),
        routed_doc_ids=list(payload["routed_doc_ids"]),
        diagnostics=dict(payload["diagnostics"]),
    )
