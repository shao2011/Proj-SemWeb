from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections import Counter
from typing import Literal, Protocol

from pydantic import Field
from qdrant_client import QdrantClient, models

from rag_ingestion.chunking import Chunk, ChunkLevel
from rag_ingestion.language import segment_thai
from rag_ingestion.models import StrictModel

ROUTING_LEVELS = (ChunkLevel.SECTION, ChunkLevel.DOCUMENT)


def _check_vector(vector: list[float], *, dimensions: int, subject: str) -> None:
    """Refuse a vector Qdrant cannot use, and say which one it was.

    The dimension check alone was not enough. A NaN or an infinity from a
    provider passes it, and then either Qdrant rejects the whole request, taking
    a batch of good chunks down with one bad response, or it stores the vector
    and every similarity computed against it is undefined. Both are worse than a
    clear failure here, and neither points at the provider that caused it.
    """
    if len(vector) != dimensions:
        raise ValueError(
            f"{subject} has dimension {len(vector)}; expected {dimensions}"
        )
    for position, value in enumerate(vector):
        if not math.isfinite(value):
            raise ValueError(
                f"{subject} is not a finite vector: element {position} is {value!r}"
            )


def _point_id(chunk_id: str) -> str:
    """Qdrant point id for a chunk.

    Deterministic so a chunk can be fetched by identity without a search,
    and so re-indexing the same content overwrites its own point instead
    of accumulating duplicates.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def _excluded_doc_conditions(
    exclude_doc_ids: list[str] | None,
) -> list[models.Condition]:
    if not exclude_doc_ids:
        return []
    return [
        models.FieldCondition(
            key="doc_id",
            match=models.MatchAny(any=sorted(set(exclude_doc_ids))),
        )
    ]


def chunk_from_payload(payload: dict[str, object]) -> Chunk:
    """Rebuild a Chunk from its stored payload.

    ``index_text`` is not stored separately because it is exactly the
    breadcrumb followed by the source text.
    """
    breadcrumb = str(payload.get("breadcrumb", ""))
    source_text = str(payload.get("source_text", ""))
    span = payload.get("source_span")
    return Chunk(
        chunk_id=str(payload["chunk_id"]),
        doc_id=str(payload["doc_id"]),
        owner_user_id=str(payload["owner_user_id"]),
        project_id=str(payload["project_id"]),
        domain=payload.get("domain"),  # type: ignore[arg-type]
        level=ChunkLevel(str(payload["level"])),
        source_text=source_text,
        index_text=f"{breadcrumb}\n{source_text}" if breadcrumb else source_text,
        source_block_ids=list(payload.get("source_block_ids") or []),  # type: ignore[arg-type]
        source_span=tuple(span) if isinstance(span, list) and len(span) == 2 else None,  # type: ignore[arg-type]
        locators=list(payload.get("locators") or []),  # type: ignore[arg-type]
        confidence=float(payload.get("confidence", 1.0)),  # type: ignore[arg-type]
        language=payload.get("language"),  # type: ignore[arg-type]
        citable=bool(payload.get("citable", True)),
        parent_id=payload.get("parent_id"),  # type: ignore[arg-type]
        child_ids=list(payload.get("child_ids") or []),  # type: ignore[arg-type]
        section_path=list(payload.get("section_path") or []),  # type: ignore[arg-type]
        refs=list(payload.get("refs") or []),  # type: ignore[arg-type]
        media_refs=list(payload.get("media_refs") or []),  # type: ignore[arg-type]
    )


class EmbeddingProfile(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    version: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    task: str = Field(min_length=1)
    mode: str = Field(min_length=1)


class Embedder(Protocol):
    def embed_documents(
        self,
        items: list[str],
        profile: EmbeddingProfile,
        *,
        intent: Literal["document"],
    ) -> list[list[float]]: ...

    def embed_query(
        self,
        text: str,
        profile: EmbeddingProfile,
        *,
        intent: Literal["query"],
    ) -> list[float]: ...


_WORD_PATTERN = re.compile(r"[^\W_]+(?:[-_.:/][^\W_]+)*", re.UNICODE)
# Scripts written without word separators: CJK ideographs, kana, hangul, and
# Thai. Without the gram fallback a whole Thai sentence hashes to ONE token
# and the sparse leg can never match a query word inside it.
_GRAM_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0e00-\u0e7f]"
)

# BM25's TF side, computed client-side: Qdrant only supplies the IDF half
# (Modifier.IDF); the values stored here must already carry saturation and
# length normalization or the sparse leg degrades to raw term counts.
_BM25_K1 = 1.2
_BM25_B = 0.75
# The chunker pins leaf size to ~256 tokens by construction, so the corpus
# average is a constant of the pipeline, not a moving statistic to track.
# FastEmbed's Qdrant/bm25 encoder fixes the same constant.
_BM25_AVGDL = 256.0


_THAI_RUN_PATTERN = re.compile(r"[฀-๿]+")


def _tokens(text: str, language: str | None) -> list[str]:
    normalized = text.casefold()
    tokens = _WORD_PATTERN.findall(normalized)
    grams = _GRAM_PATTERN.findall(normalized)
    if language in {"zh", "ja", "ko", "th"} or grams:
        tokens.extend(grams)
        tokens.extend(
            "".join(grams[index : index + 2]) for index in range(len(grams) - 1)
        )
    # Dictionary words on top of the grams, not instead of them: words carry
    # the precise signal, grams keep out-of-dictionary terms matchable.
    for run in _THAI_RUN_PATTERN.findall(normalized):
        tokens.extend(segment_thai(run))
    return tokens


def _sparse_vector(
    text: str,
    language: str | None,
    *,
    intent: Literal["document", "query"],
) -> models.SparseVector:
    tokens = _tokens(text, language)
    counts: Counter[int] = Counter()
    for token in tokens:
        index = int.from_bytes(
            hashlib.blake2b(token.encode(), digest_size=4).digest(), "big"
        )
        counts[index] += 1
    if intent == "document":
        # Saturation applies to the document side only; a query is a handful
        # of terms where the raw count is the intended weight.
        norm = _BM25_K1 * (
            1 - _BM25_B + _BM25_B * (len(tokens) / _BM25_AVGDL)
        )
        weights = {
            index: (count * (_BM25_K1 + 1)) / (count + norm)
            for index, count in counts.items()
        }
    else:
        weights = {index: float(count) for index, count in counts.items()}
    ordered = sorted(weights.items())
    return models.SparseVector(
        indices=[index for index, _ in ordered],
        values=[value for _, value in ordered],
    )


class QdrantHybridIndex:
    def __init__(
        self,
        client: QdrantClient,
        collection_name: str,
        profile: EmbeddingProfile,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.profile = profile
        self._ensured = False

    def ensure_collection(self) -> None:
        # Memoized per instance: a batch upload calls this once per file, and
        # every call after the first was pure round-trip waste (one
        # get_collection plus seven create_payload_index, all with wait=True).
        if self._ensured:
            return
        expected_metadata = {
            "embedding_profile": self.profile.model_dump(mode="json")
        }
        if self.client.collection_exists(self.collection_name):
            collection = self.client.get_collection(self.collection_name)
            if collection.config.metadata != expected_metadata:
                raise ValueError(
                    f"Collection {self.collection_name!r} has a different embedding profile"
                )
        else:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self.profile.dimensions,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
                metadata=expected_metadata,
            )
        # Every field a filter names, and nothing else. A managed cluster runs
        # strict mode, where filtering an unindexed field is rejected outright
        # instead of falling back to a scan, so a missing index here is a 400
        # on search rather than a slow query.
        for field_name, field_schema in (
            ("owner_user_id", models.PayloadSchemaType.KEYWORD),
            ("project_id", models.PayloadSchemaType.KEYWORD),
            ("level", models.PayloadSchemaType.KEYWORD),
            ("chunk_id", models.PayloadSchemaType.KEYWORD),
            ("doc_id", models.PayloadSchemaType.KEYWORD),
            ("source_block_ids", models.PayloadSchemaType.KEYWORD),
            ("citable", models.PayloadSchemaType.BOOL),
        ):
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=field_schema,
                wait=True,
            )
        self._ensured = True

    def upsert(self, chunks: list[Chunk], embedder: Embedder) -> None:
        self.ensure_collection()
        if not chunks:
            return
        dense_vectors = embedder.embed_documents(
            [chunk.index_text for chunk in chunks],
            self.profile,
            intent="document",
        )
        if len(dense_vectors) != len(chunks):
            raise ValueError("Embedder returned a different number of vectors than chunks")

        points: list[models.PointStruct] = []
        for chunk, dense_vector in zip(chunks, dense_vectors, strict=True):
            _check_vector(
                dense_vector,
                dimensions=self.profile.dimensions,
                subject=f"Dense vector for {chunk.chunk_id}",
            )
            point_id = _point_id(chunk.chunk_id)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector={
                        "dense": dense_vector,
                        "sparse": _sparse_vector(
                            chunk.index_text, chunk.language, intent="document"
                        ),
                    },
                    payload=self._payload(chunk),
                )
            )
        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )

    def _tenant_conditions(
        self, owner_user_id: str, project_id: str
    ) -> list[models.FieldCondition]:
        return [
            models.FieldCondition(
                key="owner_user_id",
                match=models.MatchValue(value=owner_user_id),
            ),
            models.FieldCondition(
                key="project_id",
                match=models.MatchValue(value=project_id),
            ),
        ]

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> None:
        """Rewrite the tenant payload of every point the anonymous id owns.

        Claiming must move the vectors too: retrieval filters on
        ``owner_user_id``, so rows moved in Postgres without this would stop
        matching their own chunks. Same missing-collection rule as the other
        write paths that must not create the collection.
        """
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.set_payload(
            collection_name=self.collection_name,
            payload={"owner_user_id": verified_user_id},
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="owner_user_id",
                        match=models.MatchValue(value=anonymous_user_id),
                    )
                ]
            ),
            wait=True,
        )

    def reassign_project_owner(
        self,
        *,
        project_id: str,
        previous_owner_user_id: str,
        new_owner_user_id: str,
    ) -> None:
        """Move one project's points to a new owner, leaving others alone.

        Handing a project over changes who every read runs as, and retrieval
        filters on ``owner_user_id``. Without this the project would keep its
        documents in Postgres and silently stop finding any of them, which
        looks exactly like an empty project.

        Scoped by ``project_id`` as well as owner, unlike `reassign_owner`:
        the previous owner keeps their other projects.
        """
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.set_payload(
            collection_name=self.collection_name,
            payload={"owner_user_id": new_owner_user_id},
            points=models.Filter(
                must=self._tenant_conditions(previous_owner_user_id, project_id)
            ),
            wait=True,
        )

    def delete_by_doc(
        self, *, owner_user_id: str, project_id: str, doc_id: str
    ) -> None:
        """Remove every point of one document within its project.

        Same missing-collection rule as :meth:`delete_by_project`.
        """
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        *self._tenant_conditions(owner_user_id, project_id),
                        models.FieldCondition(
                            key="doc_id", match=models.MatchValue(value=doc_id)
                        ),
                    ]
                )
            ),
            wait=True,
        )

    def delete_by_project(self, *, owner_user_id: str, project_id: str) -> None:
        """Remove every point belonging to one owner's project.

        Checks existence instead of calling ``ensure_collection``: a delete
        for a project that was never indexed must not create the collection,
        mirroring the read path's missing-collection rule.
        """
        if not self.client.collection_exists(self.collection_name):
            return
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=self._tenant_conditions(owner_user_id, project_id)
                )
            ),
            wait=True,
        )

    def fetch_chunks(
        self,
        chunk_ids: list[str],
        *,
        owner_user_id: str,
        project_id: str,
    ) -> dict[str, Chunk]:
        """Load chunks by identity, tenant-filtered, for merge and expansion.

        Point ids are `uuid5` of the chunk id (see `upsert`), so this is a
        primary-key lookup rather than a filtered scan. That matters because
        the callers are on the hot path of every query: the Q4 ancestor walk
        and the Q6 pass that loads leaves the outcome is missing.

        `retrieve` accepts no filter, so the tenant check the scroll filter
        used to perform happens below instead. It has to happen somewhere:
        without it, anyone holding a chunk id reads another tenant's text.
        """
        if not chunk_ids:
            return {}
        wanted = list(dict.fromkeys(chunk_ids))
        records = self.client.retrieve(
            collection_name=self.collection_name,
            ids=[_point_id(chunk_id) for chunk_id in wanted],
            with_payload=True,
            with_vectors=False,
        )
        loaded: dict[str, Chunk] = {}
        for record in records:
            payload = record.payload or {}
            if "chunk_id" not in payload:
                continue
            if (
                payload.get("owner_user_id") != owner_user_id
                or payload.get("project_id") != project_id
            ):
                continue
            chunk = chunk_from_payload(payload)
            loaded[chunk.chunk_id] = chunk
        return loaded

    def fetch_by_block_ids(
        self,
        block_ids: list[str],
        *,
        owner_user_id: str,
        project_id: str,
        limit: int = 16,
        exclude_doc_ids: list[str] | None = None,
    ) -> list[Chunk]:
        """Load citable leaves that contain any of the supplied source blocks."""
        if not block_ids:
            return []
        records, _ = self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=models.Filter(
                must=[
                    *self._tenant_conditions(owner_user_id, project_id),
                    models.FieldCondition(
                        key="citable", match=models.MatchValue(value=True)
                    ),
                    models.FieldCondition(
                        key="source_block_ids",
                        match=models.MatchAny(any=list(dict.fromkeys(block_ids))),
                    ),
                ],
                must_not=_excluded_doc_conditions(exclude_doc_ids),
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            chunk_from_payload(record.payload or {})
            for record in records
            if (record.payload or {}).get("chunk_id")
        ]

    def hybrid_search(
        self,
        query: str,
        embedder: Embedder,
        *,
        owner_user_id: str,
        project_id: str,
        language: str | None = None,
        limit: int = 10,
        levels: tuple[ChunkLevel, ...] | None = None,
        citable: bool | None = True,
        doc_ids: list[str] | None = None,
        exclude_doc_ids: list[str] | None = None,
        dense_vector: list[float] | None = None,
    ) -> list[models.ScoredPoint]:
        # No ensure_collection here: a read must neither create the collection
        # nor pay its index round-trips. Callers guard collection existence.
        if dense_vector is None:
            dense_vector = embedder.embed_query(
                query,
                self.profile,
                intent="query",
            )
        _check_vector(
            dense_vector,
            dimensions=self.profile.dimensions,
            subject="Query vector",
        )
        conditions: list[models.Condition] = list(
            self._tenant_conditions(owner_user_id, project_id)
        )
        if citable is not None:
            conditions.append(
                models.FieldCondition(
                    key="citable", match=models.MatchValue(value=citable)
                )
            )
        if levels:
            conditions.append(
                models.FieldCondition(
                    key="level",
                    match=models.MatchAny(any=[level.value for level in levels]),
                )
            )
        if doc_ids:
            conditions.append(
                models.FieldCondition(
                    key="doc_id",
                    match=models.MatchAny(any=list(dict.fromkeys(doc_ids))),
                )
            )
        query_filter = models.Filter(
            must=conditions,
            must_not=_excluded_doc_conditions(exclude_doc_ids),
        )
        response = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense_vector,
                    using="dense",
                    filter=query_filter,
                    limit=max(limit * 2, limit),
                ),
                models.Prefetch(
                    query=_sparse_vector(query, language, intent="query"),
                    using="sparse",
                    filter=query_filter,
                    limit=max(limit * 2, limit),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return response.points

    def _payload(self, chunk: Chunk) -> dict[str, object]:
        return {
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "owner_user_id": chunk.owner_user_id,
            "project_id": chunk.project_id,
            "domain": chunk.domain,
            "level": chunk.level.value,
            "source_text": chunk.source_text,
            "source_block_ids": chunk.source_block_ids,
            "source_span": list(chunk.source_span) if chunk.source_span else None,
            "locators": [locator.model_dump(mode="json") for locator in chunk.locators],
            "confidence": chunk.confidence,
            "language": chunk.language,
            "citable": chunk.citable,
            "parent_id": chunk.parent_id,
            "child_ids": chunk.child_ids,
            "section_path": chunk.section_path,
            "refs": chunk.refs,
            "media_refs": chunk.media_refs,
            "breadcrumb": chunk.index_text.split("\n", 1)[0],
            "embedding_provider": self.profile.provider,
            "embedding_model": self.profile.model,
            "embedding_version": self.profile.version,
            "embedding_mode": self.profile.mode,
            "embedding_task": self.profile.task,
        }
