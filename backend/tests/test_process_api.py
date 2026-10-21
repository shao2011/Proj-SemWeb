import json
from io import BytesIO
from pathlib import Path
from threading import Barrier, Lock, get_ident
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.api.routes import create_query_app
from rag_ingestion.config import ChunkingConfig, UploadConfig
from rag_ingestion.retrieval.reranking import RankedResult


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [[1.0, 0.0] for _ in items]

    def embed_query(self, text, profile, *, intent):
        return [1.0, 0.0]


class IdentityReranker:
    def rerank(self, query, documents, *, top_n):
        return [
            RankedResult(index=index, relevance_score=1.0)
            for index in range(min(top_n, len(documents)))
        ]


class ConcurrentEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.barrier = Barrier(2)
        self.lock = Lock()
        self.thread_ids: set[int] = set()

    def embed_documents(self, items, profile, *, intent):
        with self.lock:
            self.thread_ids.add(get_ident())
        self.barrier.wait(timeout=5)
        return super().embed_documents(items, profile, intent=intent)


def _docx_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Travel</w:t></w:r></w:p>
                    <w:p><w:r><w:t>Travel requests require code ZX-42.</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """,
        )
    return buffer.getvalue()


def _setup(
    pg_engine,
    *,
    max_upload_bytes: int | None = None,
    max_upload_files: int | None = None,
    max_upload_batch_bytes: int | None = None,
    upload_worker_count: int | None = None,
    chunking=None,
    retrieval_cache=None,
    embedder=None,
):
    qdrant = QdrantClient(location=":memory:")
    store = PostgresProjectStore(pg_engine)
    profile = EmbeddingProfile(
        provider="cohere",
        model="fake-2d",
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )
    bundle = ProviderBundle(
        provider=Provider.COHERE,
        profile=profile,
        collection_name="rag-cohere-v1",
        embedder=embedder or FakeEmbedder(),
        reranker=IdentityReranker(),
    )
    project = store.create_project(
        owner_user_id="user-a", name="Documents", embedding_profile=profile
    )
    limits: dict[str, int] = {}
    if max_upload_bytes is not None:
        limits["max_file_bytes"] = max_upload_bytes
    if max_upload_files is not None:
        limits["max_files_per_batch"] = max_upload_files
    if max_upload_batch_bytes is not None:
        limits["max_batch_bytes"] = max_upload_batch_bytes
    if upload_worker_count is not None:
        limits["worker_count"] = upload_worker_count
    app = create_query_app(
        qdrant,
        ProviderRegistry([bundle]),
        store,
        upload=UploadConfig(**limits) if limits else None,
        chunking=chunking,
        retrieval_cache=retrieval_cache,
    )
    return TestClient(app), qdrant, store, project.project_id


def _sse_events(response) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in response.text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[6:].strip() for line in lines if line.startswith("event:"))
        data = next(line[5:].strip() for line in lines if line.startswith("data:"))
        events.append((event, json.loads(data)))
    return events


def _single_result(response) -> dict[str, object]:
    return next(
        payload for event, payload in _sse_events(response) if event == "file_result"
    )


def test_batch_upload_processes_files_concurrently_and_streams_results(pg_engine) -> None:
    embedder = ConcurrentEmbedder()
    client, qdrant, store, project_id = _setup(
        pg_engine,
        embedder=embedder,
        upload_worker_count=2,
    )
    try:
        response = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files=[
                ("files", ("first.docx", _docx_bytes())),
                ("files", ("second.docx", _docx_bytes())),
            ],
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response)
        results = [payload for event, payload in events if event == "file_result"]
        assert {result["index"] for result in results} == {0, 1}
        assert {result["status"] for result in results} == {"completed"}
        assert events[-1] == (
            "done",
            {"total": 2, "completed": 2, "failed": 0, "duplicates": 0, "restored": 0},
        )
        assert len(embedder.thread_ids) == 2
        assert qdrant.collection_exists("rag-cohere-v1") is True
    finally:
        store.close()


def test_batch_upload_failure_does_not_block_other_files(pg_engine) -> None:
    client, _, store, project_id = _setup(pg_engine, upload_worker_count=2)
    try:
        response = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files=[
                ("files", ("broken.docx", b"not a zip")),
                ("files", ("policy.docx", _docx_bytes())),
            ],
        )

        assert response.status_code == 200
        events = _sse_events(response)
        results = {
            payload["filename"]: payload
            for event, payload in events
            if event == "file_result"
        }
        assert results["broken.docx"]["status"] == "failed"
        assert results["policy.docx"]["status"] == "completed"
        assert events[-1] == (
            "done",
            {"total": 2, "completed": 1, "failed": 1, "duplicates": 0, "restored": 0},
        )
        records = store.list_files(owner_user_id="user-a", project_id=project_id)
        assert records is not None
        assert {record.filename: record.status.value for record in records} == {
            "broken.docx": "failed",
            "policy.docx": "completed",
        }
    finally:
        store.close()


def test_batch_upload_rejects_count_and_total_size_before_streaming(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(
        pg_engine,
        max_upload_files=1,
        max_upload_batch_bytes=10,
    )
    try:
        too_many = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files=[
                ("files", ("first.docx", b"1234")),
                ("files", ("second.docx", b"5678")),
            ],
        )
        too_large = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("large.docx", b"12345678901")},
        )

        assert too_many.status_code == 413
        assert too_many.json()["detail"] == "Batch exceeds the 1-file limit"
        assert too_large.status_code == 413
        assert too_large.json()["detail"] == "Batch exceeds the 10-byte limit"
        assert qdrant.collection_exists("rag-cohere-v1") is False
    finally:
        store.close()


def test_project_upload_indexes_updates_metadata_and_becomes_queryable(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        response = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("policy.docx", _docx_bytes())},
        )
        assert response.status_code == 200
        body = _single_result(response)
        assert body["filename"] == "policy.docx"
        assert body["status"] == "completed"
        assert body["block_count"] == 2
        assert body["chunk_count"] == 3
        assert qdrant.collection_exists("rag-cohere-v1") is True
        files = client.get(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
        ).json()
        assert files[0]["file_id"] == body["file_id"]
        assert files[0]["doc_id"] == body["doc_id"]
        assert files[0]["status"] == "completed"

        query = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42"},
        )
        assert query.status_code == 200
        assert query.json()["hits"][0]["source_text"] == "Travel requests require code ZX-42."
    finally:
        store.close()


def test_project_upload_rejects_unsupported_blank_user_empty_and_oversized(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(pg_engine, max_upload_bytes=8)
    try:
        unsupported = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("notes.txt", b"plain text")},
        )
        blank_user = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "   "},
            files={"files": ("policy.docx", _docx_bytes())},
        )
        empty = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("empty.docx", b"")},
        )
        oversized = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("large.docx", b"123456789")},
        )
        assert unsupported.status_code == 200
        assert blank_user.status_code == 422
        assert empty.status_code == 200
        assert oversized.status_code == 200
        assert _single_result(unsupported)["status"] == "failed"
        assert _single_result(empty)["detail"] == "Uploaded file is empty"
        assert "8-byte limit" in str(_single_result(oversized)["detail"])
        assert qdrant.collection_exists("rag-cohere-v1") is False
    finally:
        store.close()


def test_project_upload_records_profile_mismatch_as_service_failure(
    pg_engine, caplog
) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    incompatible = EmbeddingProfile(
        provider="cohere",
        model="other-model",
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )
    QdrantHybridIndex(qdrant, "rag-cohere-v1", incompatible).ensure_collection()
    try:
        with caplog.at_level("ERROR", logger="rag_ingestion.api.routers.projects"):
            response = client.post(
                f"/projects/{project_id}/documents",
                headers={"X-User-ID": "user-a"},
                    files={"files": ("policy.docx", _docx_bytes())},
            )
        assert response.status_code == 503
        files = client.get(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
        ).json()
        assert files == []

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        assert project_id in error_records[0].getMessage()
        assert error_records[0].exc_info is not None
    finally:
        store.close()


def test_project_upload_records_unexpected_index_failure_as_terminal(
    pg_engine, caplog
) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    qdrant.upsert = Mock(side_effect=RuntimeError("connection lost"))
    try:
        with caplog.at_level("ERROR", logger="rag_ingestion.api.routers.projects"):
            response = client.post(
                f"/projects/{project_id}/documents",
                headers={"X-User-ID": "user-a"},
                    files={"files": ("policy.docx", _docx_bytes())},
            )
        assert response.status_code == 200
        assert _single_result(response)["status"] == "failed"
        files = client.get(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
        ).json()
        assert files[0]["status"] == "failed"
        assert files[0]["error"] == "Index request failed"

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        assert "policy.docx" in error_records[0].getMessage()
        assert error_records[0].exc_info is not None
        assert error_records[0].exc_info[0] is RuntimeError
    finally:
        store.close()


def test_project_upload_logs_the_real_exception_on_processing_failure(
    pg_engine, caplog
) -> None:
    """The 422 the caller sees is generic on purpose; the log is where the
    actual parser exception (BadZipFile, KeyError, ...) has to show up, or a
    corrupt file is undebuggable from the server side."""
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        with caplog.at_level("ERROR", logger="rag_ingestion.api.routers.projects"):
            response = client.post(
                f"/projects/{project_id}/documents",
                headers={"X-User-ID": "user-a"},
                    files={"files": ("broken.docx", b"not actually a zip file")},
            )
        assert response.status_code == 200
        assert _single_result(response)["status"] == "failed"
        files = client.get(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
        ).json()
        assert files[0]["status"] == "failed"
        assert files[0]["error"] == "Document processing failed"

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) == 1
        message = error_records[0].getMessage()
        assert "broken.docx" in message
        assert project_id in message
        assert error_records[0].exc_info is not None
    finally:
        store.close()


def test_project_upload_rejects_a_corrupt_pdf_instead_of_500ing(pg_engine) -> None:
    """A malformed PDF makes pymupdf raise its own `FzErrorBase` family, not
    one of the exception types this route already catches. Uncaught, that
    exception both 500s the request and leaves the file record stuck at
    "processing" forever, since the route never reaches the code that marks
    it failed."""
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        response = client.post(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
            files={"files": ("broken.pdf", b"not actually a pdf file")},
        )
        assert response.status_code == 200
        assert _single_result(response)["status"] == "failed"
        files = client.get(
            f"/projects/{project_id}/documents",
            headers={"X-User-ID": "user-a"},
        ).json()
        assert files[0]["status"] == "failed"
        assert files[0]["error"] == "Document processing failed"
    finally:
        store.close()


def test_chunking_config_reaches_the_ingest_pipeline(pg_engine) -> None:
    """Same proof as the retrieval one, at the other end of the pipeline.

    `chunk_document` accepted a config and the upload route never passed one,
    so leaf sizing was fixed at the defaults regardless of configuration.
    Forcing a tiny leaf must produce more chunks than the default run.
    """
    default_client, _, _, default_project = _setup(pg_engine)
    default_result = _single_result(
        default_client.post(
            f"/projects/{default_project}/documents",
            headers={"X-User-ID": "user-a"},
            files=[("files", ("policy.docx", _docx_bytes()))],
        )
    )

    tiny_client, _, _, tiny_project = _setup(
        pg_engine,
        chunking=ChunkingConfig(
            leaf_min_tokens=1, leaf_target_tokens=1, leaf_max_tokens=2
        ),
    )
    tiny_result = _single_result(
        tiny_client.post(
            f"/projects/{tiny_project}/documents",
            headers={"X-User-ID": "user-a"},
            files=[("files", ("policy.docx", _docx_bytes()))],
        )
    )

    assert default_result["status"] == "completed"
    assert tiny_result["status"] == "completed"
    assert tiny_result["chunk_count"] > default_result["chunk_count"], (
        "the configured leaf size did not reach chunk_document"
    )


class _CountingRedis:
    """Real-enough Redis for the cache: one keyspace shared by get/setex/incr."""

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


def test_a_completed_upload_invalidates_cached_retrieval(pg_engine) -> None:
    """The correctness half of the retrieval cache.

    A cached outcome computed before an upload cannot contain the document
    that just landed. Serving it would answer with a confident citation to the
    older evidence, which is worse than answering slowly.
    """
    from rag_ingestion.retrieval.cache import RetrievalCache

    redis = _CountingRedis()
    cache = RetrievalCache(redis, ttl_seconds=300)
    client, _, store, project_id = _setup(pg_engine, retrieval_cache=cache)

    version_key = f"retrieval:index_version:v1:{project_id}"
    assert version_key not in redis.store, "no version until something is indexed"

    response = client.post(
        f"/projects/{project_id}/documents",
        headers={"X-User-ID": "user-a"},
        files=[("files", ("policy.docx", _docx_bytes()))],
    )

    assert _single_result(response)["status"] == "completed"
    assert redis.store[version_key] == "1", "a completed upload must bump the version"


def test_a_failed_upload_does_not_invalidate(pg_engine) -> None:
    """Nothing was indexed, so nothing cached became wrong. Bumping anyway
    would throw away every valid entry for the project on a bad file."""
    from rag_ingestion.retrieval.cache import RetrievalCache

    redis = _CountingRedis()
    cache = RetrievalCache(redis, ttl_seconds=300)
    client, _, store, project_id = _setup(pg_engine, retrieval_cache=cache)

    response = client.post(
        f"/projects/{project_id}/documents",
        headers={"X-User-ID": "user-a"},
        files=[("files", ("broken.docx", b"not a zip"))],
    )

    assert _single_result(response)["status"] == "failed"
    assert f"retrieval:index_version:v1:{project_id}" not in redis.store


def _upload(client, project_id: str, name: str = "guide.docx", content: bytes | None = None):
    return client.post(
        f"/projects/{project_id}/documents",
        headers={"X-User-ID": "user-a"},
        files=[("files", (name, content or _docx_bytes()))],
    )


def test_document_delete_trashes_then_query_excludes_then_reupload_restores(
    pg_engine,
) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        uploaded = _single_result(_upload(client, project_id))
        assert uploaded["status"] == "completed"
        file_id, doc_id = uploaded["file_id"], uploaded["doc_id"]

        hits = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42"},
        ).json()["hits"]
        assert hits, "sanity: document is retrievable before the delete"

        deleted = client.delete(
            f"/projects/{project_id}/documents/{file_id}",
            headers={"X-User-ID": "user-a"},
        )
        assert deleted.status_code == 204

        listed = client.get(
            f"/projects/{project_id}/documents", headers={"X-User-ID": "user-a"}
        ).json()
        assert listed == [], "trashed documents must leave the listing"

        hits_after = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42"},
        ).json()["hits"]
        assert hits_after == [], "trashed documents must leave retrieval"

        records, _ = qdrant.scroll("rag-cohere-v1", limit=50)
        assert records, "trash keeps the vectors for the restore window"

        markdown = client.get(
            f"/projects/{project_id}/documents/{doc_id}/markdown",
            headers={"X-User-ID": "user-a"},
        )
        assert markdown.status_code in (200, 404)  # 404 only when no media store

        restored = _single_result(_upload(client, project_id))
        assert restored["status"] == "restored"
        assert restored["file_id"] == file_id

        hits_restored = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42"},
        ).json()["hits"]
        assert hits_restored, "restore must bring the document back instantly"
    finally:
        store.close()


def test_duplicate_upload_is_reported_not_reindexed(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        first = _single_result(_upload(client, project_id))
        assert first["status"] == "completed"

        again = _single_result(_upload(client, project_id, name="renamed.docx"))
        assert again["status"] == "duplicate"
        assert again["file_id"] == first["file_id"]
        assert "guide.docx" in str(again["detail"])

        listed = client.get(
            f"/projects/{project_id}/documents", headers={"X-User-ID": "user-a"}
        ).json()
        assert len(listed) == 1, "a duplicate must not create a second row"
    finally:
        store.close()


def test_permanent_delete_purges_vectors_and_row(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        uploaded = _single_result(_upload(client, project_id))
        file_id = uploaded["file_id"]

        deleted = client.delete(
            f"/projects/{project_id}/documents/{file_id}?permanent=true",
            headers={"X-User-ID": "user-a"},
        )
        assert deleted.status_code == 204

        records, _ = qdrant.scroll("rag-cohere-v1", limit=50)
        assert records == [], "permanent delete must remove the vectors"

        reuploaded = _single_result(_upload(client, project_id))
        assert reuploaded["status"] == "completed", (
            "after a purge the same content is a fresh index, not a restore"
        )
    finally:
        store.close()


def test_expired_trash_is_purged_and_reupload_indexes_fresh(pg_engine) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlmodel import Session, select

    from rag_ingestion.db.tables import ProjectFileRow

    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        uploaded = _single_result(_upload(client, project_id))
        file_id = uploaded["file_id"]
        client.delete(
            f"/projects/{project_id}/documents/{file_id}",
            headers={"X-User-ID": "user-a"},
        )
        with Session(pg_engine) as session:
            row = session.exec(
                select(ProjectFileRow).where(ProjectFileRow.file_id == file_id)
            ).one()
            row.trashed_at = datetime.now(UTC) - timedelta(days=30)
            session.add(row)
            session.commit()

        reuploaded = _single_result(_upload(client, project_id))
        assert reuploaded["status"] == "completed"
        assert reuploaded["file_id"] != file_id, "the expired row must be purged"

        listed = client.get(
            f"/projects/{project_id}/documents", headers={"X-User-ID": "user-a"}
        ).json()
        assert [item["file_id"] for item in listed] == [reuploaded["file_id"]]
    finally:
        store.close()


def test_query_respects_caller_disabled_doc_ids(pg_engine) -> None:
    client, qdrant, store, project_id = _setup(pg_engine)
    try:
        uploaded = _single_result(_upload(client, project_id))
        doc_id = uploaded["doc_id"]

        with_doc = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42"},
        ).json()["hits"]
        assert with_doc

        without_doc = client.post(
            f"/projects/{project_id}/query",
            headers={"X-User-ID": "user-a"},
            json={"query": "ZX-42", "disabled_doc_ids": [doc_id]},
        ).json()["hits"]
        assert without_doc == []
    finally:
        store.close()
