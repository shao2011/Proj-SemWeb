"""Read routes for a document's rendered markdown and its media."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from rag_ingestion.api.routes import create_query_app
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.storage.media import LocalMediaStore
from rag_ingestion.storage.pg_projects import PostgresProjectStore


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


def _docx_bytes(body: str = "Travel requests require code ZX-42.") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            f"""
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Travel</w:t></w:r></w:p>
                    <w:p><w:r><w:t>{body}</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """,
        )
    return buffer.getvalue()


_XLSX_PICTURE = b"\x89PNG\r\n\x1a\n" + b"qr-code-bytes" * 4

_XLSX_DRAWING = """<?xml version="1.0" encoding="UTF-8"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <xdr:twoCellAnchor>
    <xdr:from><xdr:col>0</xdr:col><xdr:row>0</xdr:row></xdr:from>
    <xdr:to><xdr:col>3</xdr:col><xdr:row>5</xdr:row></xdr:to>
    <xdr:pic>
      <xdr:nvPicPr><xdr:cNvPr id="2" name="QR code"/><xdr:cNvPicPr/></xdr:nvPicPr>
      <xdr:blipFill>
        <a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="rId1"/>
      </xdr:blipFill>
    </xdr:pic>
  </xdr:twoCellAnchor>
</xdr:wsDr>
"""

_XLSX_DRAWING_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                Target="../media/image1.png"/>
</Relationships>
"""

_XLSX_SHEET_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
                Target="../drawings/drawing1.xml"/>
</Relationships>
"""


def _xlsx_bytes() -> bytes:
    """A one-sheet workbook with a picture sitting on it."""
    from openpyxl import Workbook

    buffer = BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "QR"
    sheet["A1"] = "Chuyển khoản"
    sheet["B1"] = 38
    workbook.save(buffer)

    with ZipFile(BytesIO(buffer.getvalue())) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/media/image1.png"] = _XLSX_PICTURE
    parts["xl/drawings/drawing1.xml"] = _XLSX_DRAWING.encode()
    parts["xl/drawings/_rels/drawing1.xml.rels"] = _XLSX_DRAWING_RELS.encode()
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = _XLSX_SHEET_RELS.encode()

    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


def _setup(pg_engine, tmp_path, *, media_store=None, owner="user-a"):
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
        embedder=FakeEmbedder(),
        reranker=IdentityReranker(),
    )
    project = store.create_project(
        owner_user_id=owner, name="Documents", embedding_profile=profile
    )
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([bundle]),
        store,
        media_store=media_store if media_store is not None else LocalMediaStore(tmp_path),
    )
    return TestClient(app), store, project.project_id


def _upload(client, project_id, *, owner="user-a", filename="policy.docx", body=None) -> str:
    response = client.post(
        f"/projects/{project_id}/documents",
        headers={"X-User-ID": owner},
        files=[("files", (filename, _docx_bytes(body) if body else _docx_bytes()))],
    )
    assert response.status_code == 200, response.text
    doc_id = next(
        line.split('"doc_id":"')[1].split('"')[0]
        for line in response.text.splitlines()
        if '"doc_id":"' in line
    )
    return doc_id


def _upload_xlsx(client, project_id, *, owner="user-a") -> str:
    response = client.post(
        f"/projects/{project_id}/documents",
        headers={"X-User-ID": owner},
        files=[("files", ("ledger.xlsx", _xlsx_bytes()))],
    )
    assert response.status_code == 200, response.text
    return next(
        line.split('"doc_id":"')[1].split('"')[0]
        for line in response.text.splitlines()
        if '"doc_id":"' in line
    )


def test_uploading_a_document_makes_its_markdown_readable(pg_engine, tmp_path) -> None:
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id)

    response = client.get(
        f"/projects/{project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-a"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["doc_id"] == doc_id
    assert "Travel requests require code ZX-42." in payload["markdown"]
    assert payload["anchors"], "a document with prose must expose anchors"


def test_anchors_slice_the_returned_markdown(pg_engine, tmp_path) -> None:
    """End to end, the offsets served must index the markdown served -- that is
    the only thing a citation click depends on."""
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id, body="Nghỉ phép theo Điều 3.")

    payload = client.get(
        f"/projects/{project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-a"},
    ).json()

    markdown = payload["markdown"]
    sliced = {
        markdown[start:end] for start, end in payload["anchors"].values()
    }
    assert "Nghỉ phép theo Điều 3." in sliced


def test_another_user_cannot_read_the_markdown(pg_engine, tmp_path) -> None:
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id)

    response = client.get(
        f"/projects/{project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-b"},
    )

    assert response.status_code == 404


def test_an_unknown_doc_id_is_not_found(pg_engine, tmp_path) -> None:
    client, _, project_id = _setup(pg_engine, tmp_path)
    _upload(client, project_id)

    response = client.get(
        f"/projects/{project_id}/documents/doc_deadbeef/markdown",
        headers={"X-User-ID": "user-a"},
    )

    assert response.status_code == 404


def test_a_matching_etag_is_answered_with_304(pg_engine, tmp_path) -> None:
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id)
    url = f"/projects/{project_id}/documents/{doc_id}/markdown"
    first = client.get(url, headers={"X-User-ID": "user-a"})
    etag = first.headers["etag"]

    second = client.get(url, headers={"X-User-ID": "user-a", "If-None-Match": etag})

    assert second.status_code == 304
    assert second.headers["etag"] == etag


def test_media_is_served_to_the_owner(pg_engine, tmp_path) -> None:
    media = LocalMediaStore(tmp_path)
    client, _, project_id = _setup(pg_engine, tmp_path, media_store=media)
    doc_id = _upload(client, project_id)
    key = f"{doc_id}/page/{'a' * 16}.png"
    media.put(key, b"\x89PNG-bytes", content_type="image/png")

    response = client.get(
        f"/projects/{project_id}/media/{key}", headers={"X-User-ID": "user-a"}
    )

    assert response.status_code == 200
    assert response.content == b"\x89PNG-bytes"
    assert response.headers["content-type"].startswith("image/png")


def test_a_spreadsheet_picture_is_reachable_from_its_own_markdown(
    pg_engine, tmp_path
) -> None:
    """The whole contract the document panel relies on.

    The markdown holds a media key rather than a URL, so the panel can only
    show the picture if that exact key round-trips through the media route.
    """
    media = LocalMediaStore(tmp_path / "media")
    client, _, project_id = _setup(pg_engine, tmp_path, media_store=media)
    doc_id = _upload_xlsx(client, project_id)

    markdown = client.get(
        f"/projects/{project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-a"},
    ).json()["markdown"]
    key = markdown.split("](")[1].split(")")[0]

    response = client.get(
        f"/projects/{project_id}/media/{key}", headers={"X-User-ID": "user-a"}
    )

    assert key.startswith(f"{doc_id}/figure/")
    assert response.status_code == 200
    assert response.content == _XLSX_PICTURE
    assert response.headers["content-type"] == "image/png"


def test_media_belonging_to_another_user_is_not_found(pg_engine, tmp_path) -> None:
    media = LocalMediaStore(tmp_path)
    client, _, project_id = _setup(pg_engine, tmp_path, media_store=media)
    doc_id = _upload(client, project_id)
    key = f"{doc_id}/page/{'a' * 16}.png"
    media.put(key, b"\x89PNG-bytes", content_type="image/png")

    response = client.get(
        f"/projects/{project_id}/media/{key}", headers={"X-User-ID": "user-b"}
    )

    assert response.status_code == 404


def test_a_traversal_shaped_key_is_rejected_by_the_route(pg_engine, tmp_path) -> None:
    """Rejected on shape, before the store is asked -- an object-storage backend
    has no filesystem root to fall back on.

    The dots are percent-encoded because an HTTP client collapses `..` out of
    the path before sending; encoded, they survive routing and arrive at the
    handler as a real key, which is what an attacker would send.
    """
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id)

    for key in (
        f"{doc_id}/%2e%2e/%2e%2e/etc/passwd",
        f"{doc_id}/page/%2e%2e/%2e%2e/secret.png",
        "%2e%2e/%2e%2e/etc/passwd",
        f"{doc_id}/page/..%5c..%5csecret.png",
    ):
        response = client.get(
            f"/projects/{project_id}/media/{key}", headers={"X-User-ID": "user-a"}
        )
        assert response.status_code == 404, key
        assert response.json()["detail"] == "Media not found", (
            f"{key} was not rejected by the media route itself"
        )


def test_a_media_key_cannot_be_read_through_someone_elses_project(
    pg_engine, tmp_path
) -> None:
    """The realistic attack: keys are derived from content the uploader has, so
    a second tenant can construct one and try it against their own project."""
    media = LocalMediaStore(tmp_path)
    client, store, project_id = _setup(pg_engine, tmp_path, media_store=media)
    doc_id = _upload(client, project_id)
    key = f"{doc_id}/page/{'a' * 16}.png"
    media.put(key, b"\x89PNG-bytes", content_type="image/png")

    intruder = store.create_project(
        owner_user_id="user-b",
        name="Mine",
        embedding_profile=store.get_project(
            owner_user_id="user-a", project_id=project_id
        ).embedding_profile,
    )
    response = client.get(
        f"/projects/{intruder.project_id}/media/{key}",
        headers={"X-User-ID": "user-b"},
    )

    assert response.status_code == 404
    assert response.content != b"\x89PNG-bytes"


def test_every_retrieved_hit_resolves_to_a_range_in_the_served_markdown(
    pg_engine, tmp_path
) -> None:
    """The end of the chain, checked end to end.

    A citation is built from a hit's `source_block_ids`, so if a retrieved hit
    names a block the markdown has no anchor for, clicking that citation lands
    nowhere. Real parse, real chunking, real index, real routes.
    """
    client, _, project_id = _setup(pg_engine, tmp_path)
    doc_id = _upload(client, project_id)

    hits = client.post(
        f"/projects/{project_id}/query",
        headers={"X-User-ID": "user-a"},
        json={"query": "travel", "limit": 5},
    ).json()["hits"]
    assert hits, "retrieval returned nothing to check"

    payload = client.get(
        f"/projects/{project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-a"},
    ).json()
    anchors = payload["anchors"]

    for hit in hits:
        assert hit["doc_id"] == doc_id
        assert hit["source_block_ids"], "a citable hit must name its blocks"
        for block_id in hit["source_block_ids"]:
            assert block_id in anchors, f"{block_id} has no anchor"
            start, end = anchors[block_id]
            assert payload["markdown"][start:end].strip()


def test_markdown_is_absent_when_no_media_store_is_configured(pg_engine, tmp_path) -> None:
    """Documents indexed without a store keep working; only the panel is empty."""
    store = PostgresProjectStore(pg_engine)
    profile = EmbeddingProfile(
        provider="cohere", model="fake-2d", version="v1",
        dimensions=2, task="retrieval", mode="naive",
    )
    bundle = ProviderBundle(
        provider=Provider.COHERE, profile=profile, collection_name="rag-cohere-v1",
        embedder=FakeEmbedder(), reranker=IdentityReranker(),
    )
    project = store.create_project(
        owner_user_id="user-a", name="Documents", embedding_profile=profile
    )
    client = TestClient(
        create_query_app(QdrantClient(location=":memory:"), ProviderRegistry([bundle]), store)
    )
    doc_id = _upload(client, project.project_id)

    response = client.get(
        f"/projects/{project.project_id}/documents/{doc_id}/markdown",
        headers={"X-User-ID": "user-a"},
    )

    assert response.status_code == 404
