"""What the backend does with a provider response it cannot trust.

Every case here used to be accepted quietly: a reranker index that selected the
wrong chunk, a vector Qdrant cannot compute against, an OCR page the document
does not have, or a document nothing was read from reported as fully reliable.
The failure the tests assert on is the point, a clear one at the boundary beats
a wrong answer downstream.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import fitz
import httpx
import pytest
from qdrant_client import QdrantClient

from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.ingestion.parsers.datalab import DatalabClient
from rag_ingestion.ingestion.parsers.image import parse_image
from rag_ingestion.ingestion.parsers.pdf import ExtractedPageBlock, parse_pdf
from rag_ingestion.models import (
    Block,
    BlockType,
    Document,
    OfficePathLocator,
    PageBBoxLocator,
    SourceType,
    TimeSpanLocator,
)
from rag_ingestion.providers.registry import ProviderResponseError
from rag_ingestion.retrieval import RetrievalConfig, run_retrieval
from rag_ingestion.retrieval.reranking import RankedResult

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_PROFILE = EmbeddingProfile(
    provider="cohere",
    model="fake-2d",
    version="v1",
    dimensions=2,
    task="retrieval",
    mode="naive",
)


class _Embedder:
    """A well-behaved embedder, so a test can spoil exactly one response."""

    def __init__(self, *, document_vector=None, query_vector=None) -> None:
        self._document_vector = document_vector
        self._query_vector = query_vector

    def embed_documents(self, items, profile, *, intent):
        if self._document_vector is not None:
            return [list(self._document_vector) for _ in items]
        return [self._vector(item) for item in items]

    def embed_query(self, text, profile, *, intent):
        if self._query_vector is not None:
            return list(self._query_vector)
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        return [float("thuế" in lowered), float("điều" in lowered)]


class _FixedReranker:
    def __init__(self, results: list[RankedResult]) -> None:
        self._results = results

    def rerank(self, query, documents, *, top_n):
        return self._results


def _chunks(texts: list[str]):
    document = Document.from_content(
        content=b"provider contract fixture",
        owner_user_id="user-a",
        project_id="project-a",
        source_type=SourceType.DOCX,
        source_uri="upload://law.docx",
        parse_engine="native",
        parse_version="1",
        title="Luật",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=order,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[
                OfficePathLocator(part="word/document.xml", path=f"/p[{order + 1}]")
            ],
        )
        for order, text in enumerate(texts)
    ]
    return chunk_document(
        document,
        blocks,
        config=ChunkingConfig(
            leaf_min_tokens=1, leaf_target_tokens=6, leaf_max_tokens=40
        ),
    ).chunks


def _index(texts: list[str], embedder: _Embedder | None = None) -> QdrantHybridIndex:
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-contract-v1", _PROFILE
    )
    index.upsert(_chunks(texts), embedder or _Embedder())
    return index


# --- reranker indexes -------------------------------------------------------


@pytest.mark.parametrize(
    "results",
    [
        pytest.param(
            [RankedResult.model_construct(index=-1, relevance_score=0.9)],
            id="negative",
        ),
        pytest.param(
            [RankedResult.model_construct(index=-3, relevance_score=0.9)],
            id="far-negative",
        ),
        pytest.param([RankedResult(index=9_999, relevance_score=0.9)], id="beyond-pool"),
        pytest.param(
            [
                RankedResult(index=0, relevance_score=0.9),
                RankedResult(index=0, relevance_score=0.8),
            ],
            id="duplicate",
        ),
    ],
)
def test_an_invalid_reranker_index_fails_loudly(results: list[RankedResult]) -> None:
    """`pool[-1]` is legal Python, which is what made a negative index silent.

    The check ran `index >= len(pool)` only, so a negative index passed it and
    then selected the last candidate in the pool, producing an answer citing a
    chunk the reranker never chose.

    The negative cases go through `model_construct` on purpose. `RankedResult`
    already pins `index` to `ge=0`, so the shipped HTTP adapter cannot emit one:
    a negative index from a provider becomes a `ValidationError` and then a
    `ProviderResponseError` there. Any other `Reranker` satisfying the Protocol
    has no such gate, and the chain should not depend on one being present, which
    is what `model_construct` reproduces.
    """
    index = _index(["Điều 5 quy định thuế.", "Điều 6 quy định phạt."])

    with pytest.raises(ValueError, match="invalid candidate index"):
        run_retrieval(
            index,
            _Embedder(),
            _FixedReranker(results),
            "thuế",
            owner_user_id="user-a",
            project_id="project-a",
            config=RetrievalConfig(),
        )


def test_the_shipped_adapter_already_refuses_a_negative_index() -> None:
    """Where a real provider's negative index is actually caught."""
    from rag_ingestion.providers.adapters import _ranked_results

    with pytest.raises(ProviderResponseError, match="invalid rerank results"):
        _ranked_results({"results": [{"index": -1, "relevance_score": 0.9}]})


# --- vectors ----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [[float("nan"), 1.0], [float("inf"), 1.0], [1.0, float("-inf")]]
)
def test_a_non_finite_document_vector_is_refused_before_qdrant(
    bad: list[float],
) -> None:
    """Dimension was the only thing checked, so a NaN reached Qdrant.

    Which either fails the whole batch on one bad response, taking every good
    chunk with it, or stores a vector every later similarity is undefined
    against.
    """
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-contract-v1", _PROFILE
    )

    with pytest.raises(ValueError, match="not a finite vector"):
        index.upsert(_chunks(["Điều 5 quy định thuế."]), _Embedder(document_vector=bad))


@pytest.mark.parametrize("bad", [[float("nan"), 1.0], [1.0, float("inf")]])
def test_a_non_finite_query_vector_is_refused_before_qdrant(bad: list[float]) -> None:
    index = _index(["Điều 5 quy định thuế."])

    with pytest.raises(ValueError, match="not a finite vector"):
        index.hybrid_search(
            "thuế",
            _Embedder(query_vector=bad),
            owner_user_id="user-a",
            project_id="project-a",
        )


def test_a_wrong_length_vector_still_reports_its_dimension() -> None:
    index = QdrantHybridIndex(
        QdrantClient(location=":memory:"), "rag-contract-v1", _PROFILE
    )

    with pytest.raises(ValueError, match="dimension 3; expected 2"):
        index.upsert(
            _chunks(["Điều 5 quy định thuế."]),
            _Embedder(document_vector=[1.0, 0.0, 0.0]),
        )


# --- locators ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bbox",
    [
        pytest.param((200.0, 10.0, 5.0, 50.0), id="x-reversed"),
        pytest.param((5.0, 50.0, 200.0, 10.0), id="y-reversed"),
        pytest.param((200.0, 50.0, 5.0, 10.0), id="both-reversed"),
    ],
)
def test_a_reversed_bbox_is_rejected(bbox: tuple[float, ...]) -> None:
    """Provenance that cannot be followed back is not provenance.

    Every field passed on its own, so a reversed box was stored and only showed
    up as an empty crop when someone clicked the citation.
    """
    with pytest.raises(ValueError, match="corners are reversed"):
        PageBBoxLocator(page=1, bbox=bbox)


def test_a_non_finite_bbox_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a finite number"):
        PageBBoxLocator(page=1, bbox=(0.0, 0.0, float("inf"), 10.0))


def test_a_degenerate_bbox_is_allowed() -> None:
    """Zero width or height is a thin box, not a reversed one."""
    assert PageBBoxLocator(page=1, bbox=(5.0, 5.0, 5.0, 5.0)).bbox == (
        5.0,
        5.0,
        5.0,
        5.0,
    )


def test_a_time_span_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        TimeSpanLocator(t0=12.0, t1=4.0)

    assert TimeSpanLocator(t0=4.0, t1=4.0).t1 == 4.0


# --- OCR pages and empty parses ---------------------------------------------


class _ScanParser:
    def __init__(self, blocks: list[ExtractedPageBlock]) -> None:
        self._blocks = blocks

    def parse_pages(self, path: Path, pages: list[int]) -> list[ExtractedPageBlock]:
        return list(self._blocks)


def _scanned_pdf(path: Path, pages: int = 1) -> None:
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=600, height=400)
        page.insert_image(page.rect, stream=_ONE_PIXEL_PNG)
    document.save(path)
    document.close()


def _parse(path: Path, scan_parser) -> object:
    return parse_pdf(
        path,
        owner_user_id="user-a",
        project_id="project-a",
        source_uri="upload://scan.pdf",
        scan_parser=scan_parser,
    )


def test_an_ocr_page_outside_the_document_is_reported_not_crashed(
    tmp_path: Path,
) -> None:
    """`probes[item.page - 1]` was reached with an unchecked page number.

    A response numbered differently than expected produced a page the document
    does not have, and the whole file failed on an `IndexError` that named
    neither the provider nor the block.
    """
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path, pages=1)
    parser = _ScanParser(
        [
            ExtractedPageBlock(
                text="Real page", page=1, bbox=(5, 10, 200, 50), confidence=0.9
            ),
            ExtractedPageBlock(
                text="Impossible page", page=7, bbox=(5, 10, 200, 50), confidence=0.9
            ),
        ]
    )

    with pytest.raises(ValueError, match=r"outside this 1-page document"):
        _parse(path, parser)


def test_a_pdf_nothing_could_be_read_from_is_a_failure(tmp_path: Path) -> None:
    """`min(..., default=1.0)` called an empty parse perfectly confident."""
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path, pages=1)

    with pytest.raises(ValueError, match="No text could be extracted"):
        _parse(path, _ScanParser([]))


def test_an_image_nothing_could_be_read_from_is_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "scan.png"
    path.write_bytes(_ONE_PIXEL_PNG)

    with pytest.raises(ValueError, match="No text could be extracted"):
        parse_image(
            path,
            owner_user_id="user-a",
            project_id="project-a",
            source_uri="upload://scan.png",
            scan_parser=_ScanParser([]),
        )


# --- Datalab payload shapes -------------------------------------------------


def _datalab(handler) -> DatalabClient:
    return DatalabClient(
        api_keys=["key-1"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval=0.0,
    )


def _submitted(payload: object) -> object:
    """A submit that succeeds, then one poll returning `payload` as the result."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"success": True, "request_check_url": "https://datalab/check/1"},
            )
        return httpx.Response(
            200, json={"status": "complete", "success": True, "json": payload}
        )

    return handler


def test_a_submit_without_a_check_url_is_a_provider_error(tmp_path: Path) -> None:
    """`initial["request_check_url"]` raised `KeyError`, which reads as a bug here."""
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)

    client = _datalab(
        lambda request: httpx.Response(200, json={"success": True, "id": "abc"})
    )

    with pytest.raises(ProviderResponseError, match="request_check_url"):
        client.parse_pages(path, [1])


@pytest.mark.parametrize("body", [[1, 2, 3], "a bare string", 7])
def test_a_submit_that_is_not_an_object_is_a_provider_error(
    tmp_path: Path, body: object
) -> None:
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)

    client = _datalab(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ProviderResponseError, match="submit response"):
        client.parse_pages(path, [1])


def test_a_poll_that_is_not_an_object_is_a_provider_error(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"success": True, "request_check_url": "https://datalab/check/1"},
            )
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(ProviderResponseError, match="poll response"):
        _datalab(handler).parse_pages(path, [1])


def test_a_block_on_an_unrequested_page_is_skipped(tmp_path: Path) -> None:
    """The old fallback guessed `page_id + 1` for any page it did not recognise.

    Skipped with a warning instead: a page this call never asked for cannot be
    placed in the document, and guessing put it on one that may not exist.
    """
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path, pages=3)
    payload = {
        "children": [
            {
                "text": "Asked for this one",
                "page": 1,
                "bbox": [1, 2, 3, 4],
                "block_type": "Text",
            },
            {
                "text": "Never asked for this one",
                "page": 40,
                "bbox": [1, 2, 3, 4],
                "block_type": "Text",
            },
        ]
    }

    blocks = _datalab(_submitted(payload)).parse_pages(path, [2])

    assert [item.text for item in blocks] == ["Asked for this one"]
    assert [item.page for item in blocks] == [2]


@pytest.mark.parametrize("bbox", [["a", 2, 3, 4], [None, 2, 3, 4], [[], 2, 3, 4]])
def test_an_unusable_bbox_is_a_provider_error(tmp_path: Path, bbox: list) -> None:
    """`float(item)` raised `TypeError`/`ValueError` straight out of the walk."""
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    payload = {"children": [{"text": "Block", "page": 0, "bbox": bbox}]}

    with pytest.raises(ProviderResponseError, match="bbox element"):
        _datalab(_submitted(payload)).parse_pages(path, [1])


def test_a_nan_bbox_is_a_provider_error(tmp_path: Path) -> None:
    """Sent as raw bytes, because JSON has no NaN literal to encode.

    `json.loads` accepts the bare token anyway, so a provider can put one on the
    wire and `float("nan")` would sail through a numeric check that only asks
    whether the cast succeeded.
    """
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    raw = b'{"children":[{"text":"Block","page":0,"bbox":[NaN,2,3,4]}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"success": True, "request_check_url": "https://datalab/check/1"},
            )
        return httpx.Response(
            200,
            content=b'{"status":"complete","success":true,"json":'
            + raw
            + b"}",
            headers={"content-type": "application/json"},
        )

    with pytest.raises(ProviderResponseError, match="not finite"):
        _datalab(handler).parse_pages(path, [1])


def test_a_non_numeric_confidence_is_a_provider_error(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    payload = {
        "children": [
            {"text": "Block", "page": 0, "bbox": [1, 2, 3, 4], "confidence": "high"}
        ]
    }

    with pytest.raises(ProviderResponseError, match="confidence"):
        _datalab(_submitted(payload)).parse_pages(path, [1])


def test_a_reversed_bbox_from_datalab_is_normalized(tmp_path: Path) -> None:
    """Corner order is a coordinate convention, not a fault worth failing on.

    Normalized here so the locator contract holds, rather than stored reversed
    and discovered when a citation is followed.
    """
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    payload = {"children": [{"text": "Block", "page": 0, "bbox": [30, 40, 10, 20]}]}

    blocks = _datalab(_submitted(payload)).parse_pages(path, [1])

    assert [item.bbox for item in blocks] == [(10.0, 20.0, 30.0, 40.0)]


def test_a_result_string_that_is_not_json_is_a_provider_error(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)

    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        _datalab(_submitted("{ this is not json")).parse_pages(path, [1])


def test_a_well_formed_response_still_parses(tmp_path: Path) -> None:
    """The guards must not have closed the happy path."""
    path = tmp_path / "scan.pdf"
    _scanned_pdf(path)
    payload = json.dumps(
        {
            "children": [
                {
                    "text": "<p>Điều 5</p>",
                    "page": 0,
                    "bbox": [1, 2, 3, 4],
                    "block_type": "SectionHeader",
                    "confidence": 0.91,
                }
            ]
        }
    )

    blocks = _datalab(_submitted(payload)).parse_pages(path, [1])

    assert len(blocks) == 1
    assert blocks[0].text == "Điều 5"
    assert blocks[0].page == 1
    assert blocks[0].block_type is BlockType.HEADING
    assert blocks[0].confidence == pytest.approx(0.91)
