import logging

import pytest
from redis.exceptions import RedisError

from rag_ingestion.chunking import Chunk, ChunkLevel
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.models import OfficePathLocator
from rag_ingestion.config import RetrievalConfig
from rag_ingestion.retrieval import RetrievalOutcome
from rag_ingestion.retrieval.cache import RetrievalCache


PROFILE = EmbeddingProfile(
    provider="cohere",
    model="embed-v4.0",
    version="v1",
    dimensions=2,
    task="retrieval",
    mode="naive",
)


class FakeRedis:
    """Enough of the Redis surface for the cache, plus failure injection.

    `incr` and `get` share one keyspace, as they do in Redis. Splitting them
    into two dicts would let a version counter be invisible to the read that
    is supposed to see it, and the invalidation tests would pass against an
    implementation that is broken in production.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail
        self.sets = 0

    def get(self, key: str):
        if self.fail:
            raise RedisError("down")
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        if self.fail:
            raise RedisError("down")
        self.sets += 1
        self.store[key] = value
        return True

    def incr(self, key: str) -> int:
        if self.fail:
            raise RedisError("down")
        current = int(self.store.get(key, 0)) + 1
        self.store[key] = str(current)
        return current


def _chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc_1",
        owner_user_id="user-a",
        project_id="prj_1",
        level=ChunkLevel.LEAF,
        source_text=text,
        index_text=f"breadcrumb\n{text}",
        source_block_ids=["blk_1"],
        locators=[OfficePathLocator(part="word/document.xml", path="/p[1]")],
        confidence=1.0,
        citable=True,
    )


def _outcome() -> RetrievalOutcome:
    chunk = _chunk("chk_1", "Điều 5 quy định đối tượng áp dụng.")
    return RetrievalOutcome(
        chunks=[chunk],
        context_chunks=[chunk],
        evidence_leaf_ids=["chk_1"],
        scores={"chk_1": 0.91},
        routed_doc_ids=["doc_1"],
        diagnostics={"routed": 1, "candidates": 3},
    )


def _cache(redis: FakeRedis) -> RetrievalCache:
    return RetrievalCache(redis, ttl_seconds=300)


def _key_args(**overrides):
    args = dict(
        owner_user_id="user-a",
        project_id="prj_1",
        query="đối tượng áp dụng",
        rewritten_query=None,
        profile=PROFILE,
        config=RetrievalConfig(),
        limit=10,
        language=None,
        reranker="rerank-v4.0-pro",
    )
    args.update(overrides)
    return args


def test_miss_then_hit_round_trips_the_outcome() -> None:
    redis = FakeRedis()
    cache = _cache(redis)
    args = _key_args()

    assert cache.get(**args) is None

    cache.set(_outcome(), **args)
    restored = cache.get(**args)

    assert restored is not None
    assert [chunk.chunk_id for chunk in restored.chunks] == ["chk_1"]
    assert restored.chunks[0].source_text == "Điều 5 quy định đối tượng áp dụng."
    assert restored.scores == {"chk_1": 0.91}
    assert restored.evidence_leaf_ids == ["chk_1"]
    assert restored.routed_doc_ids == ["doc_1"]
    assert restored.diagnostics == {"routed": 1, "candidates": 3}


@pytest.mark.parametrize(
    "difference",
    [
        {"query": "một câu khác"},
        {"rewritten_query": "bản viết lại"},
        {"limit": 5},
        {"language": "vi"},
        {"project_id": "prj_2"},
        {"owner_user_id": "user-b"},
        {"config": RetrievalConfig(max_candidates=17)},
        {"profile": PROFILE.model_copy(update={"model": "embed-v3.0"})},
        {"reranker": "rerank-2.5"},
    ],
)
def test_every_input_that_changes_the_result_changes_the_key(difference) -> None:
    """A key that ignores any of these serves a result computed for something else."""
    redis = FakeRedis()
    cache = _cache(redis)
    cache.set(_outcome(), **_key_args())

    assert cache.get(**_key_args(**difference)) is None


def test_a_new_upload_invalidates_the_project() -> None:
    redis = FakeRedis()
    cache = _cache(redis)
    args = _key_args()
    cache.set(_outcome(), **args)
    assert cache.get(**args) is not None

    cache.invalidate_project("prj_1")

    assert cache.get(**args) is None, (
        "a cached answer that cannot see a newly indexed document is worse than a slow one"
    )


def test_invalidating_one_project_leaves_another_alone() -> None:
    redis = FakeRedis()
    cache = _cache(redis)
    cache.set(_outcome(), **_key_args())
    cache.set(_outcome(), **_key_args(project_id="prj_2"))

    cache.invalidate_project("prj_1")

    assert cache.get(**_key_args()) is None
    assert cache.get(**_key_args(project_id="prj_2")) is not None


def test_redis_failure_degrades_to_a_miss_rather_than_raising() -> None:
    cache = _cache(FakeRedis(fail=True))

    assert cache.get(**_key_args()) is None
    cache.set(_outcome(), **_key_args())          # must not raise
    cache.invalidate_project("prj_1")             # must not raise


def test_a_corrupt_entry_is_treated_as_a_miss() -> None:
    redis = FakeRedis()
    cache = _cache(redis)
    args = _key_args()
    cache.set(_outcome(), **args)
    redis.store[next(iter(redis.store))] = "{not json"

    assert cache.get(**args) is None


def test_hits_and_misses_are_logged_so_the_hit_rate_can_be_measured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whether this cache earns its keep depends on a hit rate nobody has
    measured, so the numbers have to be observable from day one."""
    redis = FakeRedis()
    cache = _cache(redis)
    args = _key_args()

    with caplog.at_level(logging.INFO, logger="rag_ingestion.retrieval.cache"):
        cache.get(**args)
        cache.set(_outcome(), **args)
        cache.get(**args)

    text = caplog.text
    assert "miss" in text
    assert "hit" in text


def test_ttl_is_passed_through_to_redis() -> None:
    class RecordingRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.ttls: list[int] = []

        def setex(self, key: str, ttl: int, value: str):
            self.ttls.append(ttl)
            return super().setex(key, ttl, value)

    redis = RecordingRedis()
    RetrievalCache(redis, ttl_seconds=42).set(_outcome(), **_key_args())

    assert redis.ttls == [42]
