from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Protocol


class MediaStore(Protocol):
    """Storage for the original binary artifact behind a block.

    ``media_ref`` on a block is the key returned by :meth:`put`. Backends are
    swappable the same way embedders are: local filesystem for development,
    object storage in production.
    """

    def put(self, key: str, data: bytes, *, content_type: str) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> None: ...


def media_key(*, doc_id: str, kind: str, discriminator: str, extension: str) -> str:
    """Build a deterministic media key so re-ingesting overwrites in place."""
    digest = hashlib.sha256(discriminator.encode()).hexdigest()[:16]
    return f"{doc_id}/{kind}/{digest}.{extension.lstrip('.')}"


class LocalMediaStore:
    """Filesystem-backed media store used for local development and tests."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"Media key escapes the store root: {key!r}")
        return candidate

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path)
