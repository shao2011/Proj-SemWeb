"""Second review round: cache lifecycle and the never-raises contract.

Four behaviours, each of which held only on the happy path before:

* the cache invalidation after an upload survived only if the client stayed
  connected for the whole stream;
* the cache key ignored which reranker model scored the outcome;
* `write_understanding` promised "never raises" and did not keep it;
* reference expansion handed Qdrant more block ids than the fetch limit and
  let point-id order decide which ones were even considered.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy.exc import OperationalError

from rag_ingestion.api.app import create_query_app
from rag_ingestion.config import RetrievalConfig
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.retrieval.cache import RetrievalCache
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.schemas.understanding import MemoryCandidate, Understanding
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore


PROFILE = EmbeddingProfile(
    provider="cohere",
    model="fake-2d",
    version="v1",
    dimensions=2,
    task="retrieval",
    mode="naive",
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        return True

    def incr(self, key: str) -> int:
        current = int(self.store.get(key, 0)) + 1
        self.store[key] = str(current)
        return current


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [[1.0, 0.0] for _ in items]

    def embed_query(self, text, profile, *, intent):
        return [1.0, 0.0]


class IdentityReranker:
    model = "rerank-fake"

    def rerank(self, query, documents, *, top_n):
        return [
            RankedResult(index=index, relevance_score=1.0 - index / 100)
            for index in range(min(top_n, len(documents)))
        ]


def _headers() -> dict[str, str]:
    return {"X-User-ID": "user-a"}


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Default Extension="xml" ContentType="application/xml"/>
                  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
                </Types>
            """,
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>Travel requests require code ZX-42.</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """,
        )
    return buffer.getvalue()


def _bundle() -> ProviderBundle:
    return ProviderBundle(
        provider=Provider.COHERE,
        profile=PROFILE,
        collection_name="rag-cohere-v1",
        embedder=FakeEmbedder(),
        reranker=IdentityReranker(),
    )


# --- upload invalidation vs disconnect ---------------------------------------


def test_a_disconnect_mid_upload_still_invalidates_the_cache(pg_engine) -> None:
    """The file survives a disconnect; the invalidation has to survive with it.

    The upload futures are all submitted before the stream starts, so a client
    that goes away leaves them running to completion: the document lands in the
    index either way. The version bump sat at the end of the streamed loop, the
    one part of the route a disconnect kills, so exactly when the index changed,
    the cache was left claiming it had not.

    The response generator is driven by hand rather than through TestClient:
    the test client always drains a streaming body to the end, so it cannot
    express "the client went away". `close()` on a suspended generator is
    exactly what the server does on a disconnect -- it raises GeneratorExit at
    the current yield.
    """
    import asyncio

    from fastapi import UploadFile

    redis = FakeRedis()
    cache = RetrievalCache(redis, ttl_seconds=300)
    store = PostgresProjectStore(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Docs", embedding_profile=PROFILE
    )
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([_bundle()]),
        store,
        retrieval_cache=cache,
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/projects/{project_id}/documents"
        and "POST" in getattr(route, "methods", set())
    )
    # Two files, so the generator suspends at the first `file_result` yield
    # with work still outstanding -- the worst moment to disconnect.
    upload = [
        UploadFile(file=io.BytesIO(_docx_bytes()), filename=f"policy-{n}.docx")
        for n in range(2)
    ]

    response = endpoint(project.project_id, upload, "user-a")

    async def one_event_then_gone() -> None:
        # StreamingResponse wraps the sync generator in an async one; `aclose`
        # is what the server calls on it when the client drops, and it lands as
        # GeneratorExit at the suspended yield inside `emit`.
        body = response.body_iterator
        await body.__anext__()
        await body.aclose()

    asyncio.run(one_event_then_gone())

    version_key = f"retrieval:index_version:v1:{project.project_id}"
    assert redis.store.get(version_key) == "1"


def test_a_disconnect_over_a_failed_upload_does_not_invalidate(pg_engine) -> None:
    """The fourth corner of the matrix: gone client, nothing indexed.

    The `finally` path must read the futures' real results, not treat "the
    client left" as "something must have landed" -- a corrupt file plus a
    disconnect should leave every cached entry exactly as valid as it was.
    """
    import asyncio

    from fastapi import UploadFile

    redis = FakeRedis()
    cache = RetrievalCache(redis, ttl_seconds=300)
    store = PostgresProjectStore(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Docs", embedding_profile=PROFILE
    )
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([_bundle()]),
        store,
        retrieval_cache=cache,
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/projects/{project_id}/documents"
        and "POST" in getattr(route, "methods", set())
    )
    upload = [
        UploadFile(file=io.BytesIO(b"not a zip"), filename=f"broken-{n}.docx")
        for n in range(2)
    ]

    response = endpoint(project.project_id, upload, "user-a")

    async def one_event_then_gone() -> None:
        body = response.body_iterator
        await body.__anext__()
        await body.aclose()

    asyncio.run(one_event_then_gone())

    assert f"retrieval:index_version:v1:{project.project_id}" not in redis.store


def test_a_failed_upload_still_does_not_invalidate(pg_engine) -> None:
    """Guard for the fix above: unconditional-in-finally must not mean always.

    A rejected file indexes nothing, so no cached entry became wrong, and
    bumping anyway would throw away every valid entry for the project.
    """
    redis = FakeRedis()
    cache = RetrievalCache(redis, ttl_seconds=300)
    store = PostgresProjectStore(pg_engine)
    project = store.create_project(
        owner_user_id="user-a", name="Docs", embedding_profile=PROFILE
    )
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([_bundle()]),
        store,
        retrieval_cache=cache,
    )
    client = TestClient(app)

    response = client.post(
        f"/projects/{project.project_id}/documents",
        headers=_headers(),
        files=[("files", ("broken.docx", b"not a zip"))],
    )

    assert response.status_code == 200
    assert f"retrieval:index_version:v1:{project.project_id}" not in redis.store


# --- reranker in the cache key ------------------------------------------------


def test_a_different_reranker_model_is_a_different_cache_key() -> None:
    """The reranker decides which chunks the outcome holds and their scores.

    It was the one input missing from the key, so changing the rerank model
    kept serving rankings the old model computed, and two instances configured
    differently would read each other's results off a shared Redis.
    """
    from rag_ingestion.chunking import Chunk, ChunkLevel
    from rag_ingestion.models import OfficePathLocator
    from rag_ingestion.retrieval import RetrievalOutcome

    cache = RetrievalCache(FakeRedis(), ttl_seconds=300)
    chunk = Chunk(
        chunk_id="chk_1",
        doc_id="doc_1",
        owner_user_id="user-a",
        project_id="prj_1",
        level=ChunkLevel.LEAF,
        source_text="Điều 5.",
        index_text="Điều 5.",
        source_block_ids=["blk_1"],
        locators=[OfficePathLocator(part="word/document.xml", path="/p[1]")],
        confidence=1.0,
        citable=True,
    )
    outcome = RetrievalOutcome(
        chunks=[chunk],
        evidence_leaf_ids=["chk_1"],
        scores={"chk_1": 0.9},
    )
    args = dict(
        owner_user_id="user-a",
        project_id="prj_1",
        query="điều 5",
        rewritten_query=None,
        profile=PROFILE,
        config=RetrievalConfig(),
        limit=10,
        language=None,
        reranker="rerank-v4.0-pro",
    )

    cache.set(outcome, **args)

    assert cache.get(**args) is not None
    assert cache.get(**{**args, "reranker": "rerank-2.5"}) is None


# --- write_understanding never raises -----------------------------------------


class _Chat:
    model = "test-model"

    def complete(self, turns, *, max_tokens, sticky_key=None, model=None) -> str:
        return "ok"

    def stream(self, turns, *, max_tokens, sticky_key=None, on_usage=None, tools=None, model=None, router=None) -> Iterator[str]:
        yield "ok"


class _Understander:
    def understand(self, query, context, *, sticky_key=None, model=None, router=None) -> Understanding:
        return Understanding(
            rewritten_query=None,
            memory=[
                MemoryCandidate(type="fact", key="occupation", value="Kỹ sư điện.")
            ],
        )


class _DownMemoryStore:
    """A memory store whose database is having a bad moment.

    Only the surface `write_understanding` and recall touch. Every method
    raises the way SQLAlchemy does when Postgres is unreachable, which is the
    failure the "never raises" contract exists for.
    """

    def _down(self):
        raise OperationalError("SELECT 1", {}, RuntimeError("connection refused"))

    def recall(self, **kwargs):
        return []

    def get_settings(self, **kwargs):
        self._down()

    def add_memory(self, **kwargs):
        self._down()

    def add_proposal(self, **kwargs):
        self._down()

    def distinct_thread_count(self, **kwargs):
        self._down()


def test_a_memory_outage_does_not_take_down_the_chat_turn(pg_engine) -> None:
    """`write_understanding` says "never raises"; the route depends on it.

    It runs after the user message is committed and before the stream starts,
    so an unhandled error here is the worst kind: the question is saved, the
    answer never happens, and the thread is left holding a user message with
    no reply. The answer owes nothing to memory bookkeeping.
    """
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([]),
        PostgresProjectStore(pg_engine),
        threads=PostgresThreadStore(pg_engine),
        chat=_Chat(),
        memory=_DownMemoryStore(),
        understand=_Understander(),
    )
    client = TestClient(app)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Tôi là kỹ sư điện."},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert '"role":"assistant"' in response.text
    assert '"content":"ok"' in response.text


# --- reference truncation happens on our side ----------------------------------


def test_reference_ids_are_truncated_by_weight_before_the_fetch(monkeypatch) -> None:
    """Qdrant ignores the order of a MatchAny list and truncates by point id.

    So the only ordering that counts is which ids we send at all. With more
    references than the fetch budget can honour, the low-weight ones must be
    dropped on our side, by weight, not on Qdrant's side, by uuid.
    """
    from rag_ingestion.retrieval import run_retrieval
    from tests.test_reference_expansion import (
        _ScoreByText,
        _block,
        _document,
        _Embedder,
        _index,
    )

    document = _document()
    targets = [
        _block(document, order, f"Phụ lục bảng {order}.") for order in range(3)
    ]
    sources = [
        _block(
            document,
            3 + order,
            f"Điều {5 + order} quy định thuế.",
            target=target.block_id,
        )
        for order, target in enumerate(targets)
    ]
    index = _index([*targets, *sources], document)

    sent: list[list[str]] = []
    original = type(index).fetch_by_block_ids

    def recording(self, block_ids, **kwargs):
        sent.append(list(block_ids))
        return original(self, block_ids, **kwargs)

    monkeypatch.setattr(type(index), "fetch_by_block_ids", recording)

    # Điều 5 scores highest, Điều 7 lowest; cap 1 means a budget of 2 ids.
    run_retrieval(
        index,
        _Embedder(),
        _ScoreByText({"Điều 5": 0.9, "Điều 6": 0.5, "Điều 7": 0.1}),
        "thuế",
        owner_user_id="user-a",
        project_id="project-a",
        limit=3,
        config=RetrievalConfig(reference_cap=1, route_limit=1, global_k=10),
    )

    assert sent, "expansion should have fetched"
    sent_ids = sent[0]
    assert len(sent_ids) <= 2, sent_ids
    assert targets[0].block_id in sent_ids, "the strongest reference must survive"
    assert targets[2].block_id not in sent_ids, "the weakest must be cut on our side"
