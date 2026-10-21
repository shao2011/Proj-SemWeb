"""Cloudflare R2 implementation of :class:`~rag_ingestion.storage.media.MediaStore`.

R2 speaks the S3 API, so this is a boto3 client with three details pinned down:
the region is always ``auto``, the signature is v4, and there are no
directories -- which is what makes :meth:`R2MediaStore.delete_prefix` more than
a one-liner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from rag_ingestion.config import RuntimeConfig
from rag_ingestion.storage.media import LocalMediaStore, MediaStore

#: S3 refuses a DeleteObjects request carrying more than this many keys.
DELETE_BATCH_SIZE = 1_000

#: Error codes R2 answers with when the object is simply not there. `GetObject`
#: says `NoSuchKey`; `HeadObject` has no body to put a code in and surfaces the
#: bare status instead.
_MISSING_CODES = frozenset({"NoSuchKey", "NotFound", "404"})


def _checked(key: str) -> str:
    """Refuse keys the local store would refuse, so the two agree.

    Object storage has no root to escape from, so nothing here is a containment
    boundary the way it is for `LocalMediaStore`. It is a consistency one: a key
    that raises in development must not be silently created in production.
    """
    segments = key.split("/")
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError(f"Media key escapes the store root: {key!r}")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"Media key escapes the store root: {key!r}")
    return key


def _is_missing(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code", "")) in _MISSING_CODES


class R2MediaStore:
    """Media store backed by an R2 bucket."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_credentials(
        cls,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> R2MediaStore:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # R2 is one region behind one endpoint. A real AWS region name here
            # goes into the signature and the request is rejected.
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, bucket)

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=_checked(key),
            Body=data,
            ContentType=content_type,
        )
        return key

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=_checked(key))
        except ClientError as error:
            if _is_missing(error):
                # The media and markdown routes catch OSError to answer 404. A
                # ClientError would escape as a 500, which reads as an outage
                # rather than as a document that was never indexed.
                raise FileNotFoundError(key) from error
            raise
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=_checked(key))
        except ClientError as error:
            if _is_missing(error):
                return False
            raise
        return True

    def delete_prefix(self, prefix: str) -> None:
        # The trailing slash is load-bearing: a bare `doc-1` prefix also matches
        # `doc-10`, and there is no directory here to draw the boundary.
        folder = f"{_checked(prefix).rstrip('/')}/"
        try:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=folder
            )
            batch: list[dict[str, str]] = []
            for page in pages:
                for entry in page.get("Contents", ()):
                    batch.append({"Key": entry["Key"]})
                    if len(batch) == DELETE_BATCH_SIZE:
                        self._delete(batch)
                        batch = []
            if batch:
                self._delete(batch)
        except ClientError as error:
            # Both callers log an OSError and carry on. A botocore error would
            # escape them, so a revoked key would turn deleting a document into
            # a 500 rather than a warning and some objects left behind.
            raise OSError(f"R2 cleanup failed for {folder!r}: {error}") from error

    def _delete(self, keys: list[dict[str, str]]) -> None:
        response = self._client.delete_objects(
            Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True}
        )
        # `Quiet` suppresses the per-key success report, not the failures.
        refused = response.get("Errors") or ()
        if refused:
            raise OSError(
                "R2 refused to delete "
                + ", ".join(str(entry.get("Key")) for entry in refused)
            )

def build_media_store(config: RuntimeConfig) -> tuple[MediaStore, str]:
    """Pick the media backend from configuration, and name it for the log."""
    credentials = config.r2_credentials
    if credentials is None:
        root = Path(config.media_root)
        root.mkdir(parents=True, exist_ok=True)
        return LocalMediaStore(root), str(root)
    endpoint, bucket, access_key_id, secret_access_key = credentials
    store = R2MediaStore.from_credentials(
        endpoint=endpoint,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
    return store, f"r2:{bucket}"
