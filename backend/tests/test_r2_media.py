"""R2 media store, exercised against a fake S3 client.

The point of these is the logic that sits *around* boto3 -- prefix boundaries,
delete batching, and turning a missing object into the error the media route
already knows how to answer 404 for. Live credentials are not needed and not
used.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rag_ingestion.storage.r2_media import R2MediaStore

BUCKET = "eslothai-chat"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeS3:
    """In-memory stand-in for the subset of the S3 API the store calls."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.delete_batches: list[list[str]] = []
        self.page_size = 1_000

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        assert Bucket == BUCKET
        self.objects[Key] = (Body, ContentType)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key][0])}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        assert Bucket == BUCKET
        keys = [entry["Key"] for entry in Delete["Objects"]]
        self.delete_batches.append(keys)
        for key in keys:
            self.objects.pop(key, None)
        return {}

    def get_paginator(self, operation: str) -> "FakePaginator":
        assert operation == "list_objects_v2"
        return FakePaginator(self)


class FakePaginator:
    def __init__(self, client: FakeS3) -> None:
        self._client = client

    def paginate(self, *, Bucket: str, Prefix: str) -> Any:
        assert Bucket == BUCKET
        matches = sorted(key for key in self._client.objects if key.startswith(Prefix))
        size = self._client.page_size
        if not matches:
            # A real paginator still yields one page, without a Contents key.
            yield {}
            return
        for start in range(0, len(matches), size):
            yield {"Contents": [{"Key": key} for key in matches[start : start + size]]}


@pytest.fixture()
def store() -> tuple[R2MediaStore, FakeS3]:
    client = FakeS3()
    return R2MediaStore(client, BUCKET), client


def test_put_returns_the_key_and_keeps_the_content_type(store) -> None:
    media, client = store

    key = media.put("doc-1/page/a.png", b"binary", content_type="image/png")

    assert key == "doc-1/page/a.png"
    # The local store drops the content type; on R2 it is what the media route
    # would otherwise have to guess from the extension.
    assert client.objects[key] == (b"binary", "image/png")


def test_round_trips_bytes(store) -> None:
    media, _ = store
    media.put("doc-1/markdown/a.md", b"# heading", content_type="text/markdown")

    assert media.get("doc-1/markdown/a.md") == b"# heading"


def test_get_raises_a_filesystem_error_for_a_missing_key(store) -> None:
    media, _ = store

    # OSError, not ClientError: the media route catches OSError to answer 404,
    # and anything else leaves as a 500.
    with pytest.raises(FileNotFoundError):
        media.get("doc-1/page/missing.png")


def test_get_propagates_errors_that_are_not_a_missing_object(store) -> None:
    media, client = store

    def denied(**_: Any) -> None:
        raise _client_error("AccessDenied", "GetObject")

    client.get_object = denied  # type: ignore[method-assign]

    with pytest.raises(ClientError):
        media.get("doc-1/page/a.png")


def test_exists_reports_presence(store) -> None:
    media, _ = store
    media.put("doc-1/page/a.png", b"a", content_type="image/png")

    assert media.exists("doc-1/page/a.png") is True
    assert media.exists("doc-1/page/missing.png") is False


def test_exists_propagates_errors_that_are_not_a_missing_object(store) -> None:
    media, client = store

    def denied(**_: Any) -> None:
        raise _client_error("AccessDenied", "HeadObject")

    client.head_object = denied  # type: ignore[method-assign]

    with pytest.raises(ClientError):
        media.exists("doc-1/page/a.png")


def test_delete_prefix_removes_every_key_under_the_document(store) -> None:
    media, _ = store
    media.put("doc-1/page/a.png", b"a", content_type="image/png")
    media.put("doc-1/markdown/b.md", b"b", content_type="text/markdown")
    media.put("doc-2/page/c.png", b"c", content_type="image/png")

    media.delete_prefix("doc-1")

    assert media.exists("doc-1/page/a.png") is False
    assert media.exists("doc-1/markdown/b.md") is False
    assert media.exists("doc-2/page/c.png") is True


def test_delete_prefix_does_not_reach_a_document_whose_id_shares_the_prefix(
    store,
) -> None:
    media, _ = store
    media.put("doc-1/page/a.png", b"a", content_type="image/png")
    media.put("doc-10/page/b.png", b"b", content_type="image/png")

    media.delete_prefix("doc-1")

    # A string prefix match would take doc-10 with it. There are no directories
    # in object storage to make this boundary for us.
    assert media.exists("doc-1/page/a.png") is False
    assert media.exists("doc-10/page/b.png") is True


def test_delete_prefix_batches_at_the_thousand_key_api_limit(store) -> None:
    media, client = store
    for index in range(1_500):
        media.put(f"doc-1/page/{index:04d}.png", b"x", content_type="image/png")

    media.delete_prefix("doc-1")

    assert [len(batch) for batch in client.delete_batches] == [1_000, 500]
    assert client.objects == {}


def test_delete_prefix_spans_paginated_listings(store) -> None:
    media, client = store
    client.page_size = 400
    for index in range(900):
        media.put(f"doc-1/page/{index:04d}.png", b"x", content_type="image/png")

    media.delete_prefix("doc-1")

    assert client.objects == {}


def test_delete_prefix_is_a_no_op_for_missing_prefixes(store) -> None:
    media, client = store

    media.delete_prefix("doc-missing")

    assert client.delete_batches == []


@pytest.mark.parametrize(
    "key", ["../outside.png", "doc-1/../../outside.png", "/doc-1/page/a.png", ""]
)
def test_rejects_keys_that_would_escape_the_document_namespace(store, key: str) -> None:
    media, _ = store

    # Same contract as the local store, so a key that is refused in development
    # is not quietly accepted in production.
    with pytest.raises(ValueError, match="escapes the store root"):
        media.put(key, b"x", content_type="image/png")


def test_from_credentials_signs_for_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> FakeS3:
        captured["service"] = service
        captured.update(kwargs)
        return FakeS3()

    monkeypatch.setattr("rag_ingestion.storage.r2_media.boto3.client", fake_client)

    media = R2MediaStore.from_credentials(
        endpoint="https://account.r2.cloudflarestorage.com",
        bucket=BUCKET,
        access_key_id="key",
        secret_access_key="secret",
    )

    assert isinstance(media, R2MediaStore)
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://account.r2.cloudflarestorage.com"
    # R2 has a single region and rejects a real one in the signature.
    assert captured["region_name"] == "auto"
    assert captured["config"].signature_version == "s3v4"


def _config(**overrides: Any) -> Any:
    from rag_ingestion.config import RuntimeConfig

    return RuntimeConfig(qdrant_url="http://localhost:6333", **overrides)


def test_build_media_store_falls_back_to_local_disk(tmp_path) -> None:
    from rag_ingestion.storage.media import LocalMediaStore
    from rag_ingestion.storage.r2_media import build_media_store

    root = tmp_path / "media"
    store, target = build_media_store(_config(media_root=str(root)))

    assert isinstance(store, LocalMediaStore)
    assert target == str(root)
    assert root.is_dir()


def test_build_media_store_uses_r2_when_every_setting_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_ingestion.storage.r2_media import build_media_store

    monkeypatch.setattr(
        "rag_ingestion.storage.r2_media.boto3.client",
        lambda service, **kwargs: FakeS3(),
    )

    store, target = build_media_store(
        _config(
            r2_endpoint="https://account.r2.cloudflarestorage.com",
            r2_bucket=BUCKET,
            r2_access_key_id="key",
            r2_secret_access_key="secret",
        )
    )

    assert isinstance(store, R2MediaStore)
    assert target == f"r2:{BUCKET}"


def test_half_configured_r2_is_refused_at_startup() -> None:
    # Silently falling back would write uploads to a disk the next deploy wipes.
    with pytest.raises(ValueError, match="R2_SECRET_ACCESS_KEY"):
        _config(
            r2_endpoint="https://account.r2.cloudflarestorage.com",
            r2_bucket=BUCKET,
            r2_access_key_id="key",
        )


def test_r2_settings_come_from_the_environment() -> None:
    from rag_ingestion.config import RuntimeConfig

    config = RuntimeConfig.from_environment(
        {
            "R2_ENDPOINT": "https://account.r2.cloudflarestorage.com",
            "R2_BUCKET": BUCKET,
            "R2_ACCESS_KEY_ID": "key",
            "R2_SECRET_ACCESS_KEY": "secret",
        },
        read_env_file=False,
    )

    assert config.r2_credentials == (
        "https://account.r2.cloudflarestorage.com",
        BUCKET,
        "key",
        "secret",
    )


def test_no_r2_settings_means_no_credentials() -> None:
    from rag_ingestion.config import RuntimeConfig

    config = RuntimeConfig.from_environment({}, read_env_file=False)

    assert config.r2_credentials is None


def test_delete_prefix_reports_keys_the_bucket_refused_to_delete(store) -> None:
    media, client = store
    media.put("doc-1/page/a.png", b"a", content_type="image/png")

    def refused(*, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        return {"Errors": [{"Key": Delete["Objects"][0]["Key"], "Code": "AccessDenied"}]}

    client.delete_objects = refused  # type: ignore[method-assign]

    # Both callers log an OSError and carry on. Swallowing this would leave the
    # objects behind with nothing pointing at them and no line in the log.
    with pytest.raises(OSError, match="doc-1/page/a.png"):
        media.delete_prefix("doc-1")


def test_delete_prefix_turns_a_client_error_into_an_os_error(store) -> None:
    media, client = store

    def denied(*, Bucket: str, Prefix: str) -> Any:
        raise _client_error("AccessDenied", "ListObjectsV2")

    client.get_paginator = lambda operation: type(  # type: ignore[method-assign]
        "Paginator", (), {"paginate": staticmethod(denied)}
    )()

    # A revoked key must not turn deleting a document into a 500: the callers
    # only catch OSError.
    with pytest.raises(OSError):
        media.delete_prefix("doc-1")
