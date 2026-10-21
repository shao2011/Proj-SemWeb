from pathlib import Path

import pytest

from rag_ingestion.storage.media import LocalMediaStore, media_key


def test_media_key_is_deterministic_per_document_and_discriminator() -> None:
    first = media_key(doc_id="doc-1", kind="page", discriminator="3", extension="png")
    second = media_key(doc_id="doc-1", kind="page", discriminator="3", extension=".png")
    other = media_key(doc_id="doc-1", kind="page", discriminator="4", extension="png")

    assert first == second
    assert first != other
    assert first.startswith("doc-1/page/")
    assert first.endswith(".png")


def test_local_store_round_trips_bytes(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path)
    key = store.put("doc-1/page/a.png", b"binary", content_type="image/png")

    assert key == "doc-1/page/a.png"
    assert store.exists(key)
    assert store.get(key) == b"binary"


def test_local_store_reports_missing_keys(tmp_path: Path) -> None:
    assert LocalMediaStore(tmp_path).exists("doc-1/page/missing.png") is False


def test_local_store_rejects_keys_escaping_the_root(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes the store root"):
        store.put("../outside.png", b"x", content_type="image/png")


def test_local_store_delete_prefix_removes_every_key_under_the_document(
    tmp_path: Path,
) -> None:
    store = LocalMediaStore(tmp_path)
    store.put("doc-1/page/a.png", b"a", content_type="image/png")
    store.put("doc-1/markdown/b.md", b"b", content_type="text/markdown")
    store.put("doc-2/page/c.png", b"c", content_type="image/png")

    store.delete_prefix("doc-1")

    assert not store.exists("doc-1/page/a.png")
    assert not store.exists("doc-1/markdown/b.md")
    assert store.exists("doc-2/page/c.png")


def test_local_store_delete_prefix_is_a_no_op_for_missing_prefixes(
    tmp_path: Path,
) -> None:
    LocalMediaStore(tmp_path).delete_prefix("doc-missing")


def test_local_store_delete_prefix_rejects_prefixes_escaping_the_root(
    tmp_path: Path,
) -> None:
    store = LocalMediaStore(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes the store root"):
        store.delete_prefix("../outside")
