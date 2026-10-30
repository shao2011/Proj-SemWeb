"""E1: chunking on REQuestA.

    python -m eval.e1_chunking --configs C1 C3 --models nemotron --limit 3

C3 and C5 are the system's own chunker and retrieval chain (C5 is the system
default; C3 turns Auto-Merge off). C1, C2 and C4 change only how chunks are cut
or embedded; everything after indexing is the system's code.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient

from rag_ingestion.chunking import Chunk, ChunkingConfig, ChunkLevel, chunk_document
from rag_ingestion.chunking.chunker import _make_leaf, _sentence_spans
from rag_ingestion.config import RetrievalConfig, RuntimeConfig
from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
from rag_ingestion.ingestion.pipeline import process_document
from rag_ingestion.models import Block, BlockType, ParseResult
from rag_ingestion.retrieval import RetrievalOutcome, run_retrieval
from rag_ingestion.retrieval.answering import build_turns
from rag_ingestion.retrieval.automerge import token_count
from rag_ingestion.schemas.answering import AnswerEvidence
from scripts.benchmark.mtrag_run import index_corpus

from eval import settings
from eval.data import RequestaItem, load_requesta, pdf_for
from eval.jina import LateChunkingClient, PrecomputedEmbedder, jina_bundle, pack_groups
from eval.judge import Judge, sample
from eval.llm import ChatClient
from eval.metrics import bleu1, clean_answer, f1_score, hit_at, is_relevant, mrr_at
from eval.results import ResultStore, summarise

OWNER = "eval-harness"
PROJECT = "requesta"
_EMBED_BATCH = 96


def _content_blocks(blocks: Sequence[Block]) -> list[Block]:
    """Blocks whose text a chunk can carry: not headings, not containers."""
    containers = {block.parent_block_id for block in blocks if block.parent_block_id}
    return [
        block
        for block in sorted(blocks, key=lambda item: item.order)
        if block.type is not BlockType.HEADING
        and block.block_id not in containers
        and block.text.strip()
    ]


def _plain_leaf(parsed: ParseResult, blocks: list[Block], text: str) -> Chunk:
    """A citable leaf over `blocks` that carries no heading prefix."""
    leaf = _make_leaf(parsed.document, blocks, source_text=text)
    return leaf.model_copy(update={"index_text": text, "section_path": []})


def chunk_fixed(parsed: ParseResult, window: int = settings.FIXED_WINDOW_WORDS) -> list[Chunk]:
    """C1: consecutive windows of `window` words, blind to structure."""
    words: list[tuple[str, Block]] = [
        (word, block) for block in _content_blocks(parsed.blocks) for word in re.findall(r"\S+", block.text)
    ]
    chunks: list[Chunk] = []
    for start in range(0, len(words), window):
        piece = words[start : start + window]
        blocks = list({id(block): block for _, block in piece}.values())
        chunks.append(_plain_leaf(parsed, blocks, " ".join(word for word, _ in piece)))
    return chunks


def semantic_breaks(vectors: Sequence[Sequence[float]], percentile: float) -> list[int]:
    """Indices i where a new chunk starts at sentence i + 1."""
    if len(vectors) < 2:
        return []
    matrix = np.asarray(vectors, dtype=float)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
    distances = 1.0 - np.sum(matrix[:-1] * matrix[1:], axis=1)
    threshold = np.percentile(distances, percentile)
    return [index for index, distance in enumerate(distances) if distance > threshold]


def chunk_semantic(
    parsed: ParseResult,
    embed: Callable[[list[str]], list[list[float]]],
    percentile: float = settings.SEMANTIC_BREAK_PERCENTILE,
) -> list[Chunk]:
    """C2: embed consecutive sentences, cut where neighbours drift apart."""
    sentences: list[tuple[str, Block]] = [
        (sentence, block)
        for block in _content_blocks(parsed.blocks)
        for sentence, _ in _sentence_spans(block.text)
    ]
    if not sentences:
        return []
    texts = [sentence for sentence, _ in sentences]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        vectors.extend(embed(texts[start : start + _EMBED_BATCH]))
    cuts = set(semantic_breaks(vectors, percentile))
    chunks: list[Chunk] = []
    group: list[tuple[str, Block]] = []
    for index, item in enumerate(sentences):
        group.append(item)
        if index in cuts or index == len(sentences) - 1:
            blocks = list({id(block): block for _, block in group}.values())
            chunks.append(_plain_leaf(parsed, blocks, " ".join(text for text, _ in group)))
            group = []
    return chunks


def chunk_structure(parsed: ParseResult) -> list[Chunk]:
    """C3 / C5: the system's chunker with its default sizes."""
    config = ChunkingConfig(
        leaf_min_tokens=settings.STRUCTURE_MIN_WORDS,
        leaf_target_tokens=settings.STRUCTURE_TARGET_WORDS,
        leaf_max_tokens=settings.STRUCTURE_MAX_WORDS,
    )
    return chunk_document(parsed.document, parsed.blocks, config=config).chunks


def without_prefix(chunks: Sequence[Chunk]) -> list[Chunk]:
    """C4 indexes the C3 chunks as plain text."""
    return [chunk.model_copy(update={"index_text": chunk.source_text}) for chunk in chunks]


def section_groups(chunks: Sequence[Chunk]) -> list[list[str]]:
    """Leaf texts grouped by consecutive section, in document order."""
    groups: list[list[str]] = []
    key: tuple | None = None
    for chunk in chunks:
        if chunk.level is not ChunkLevel.LEAF:
            continue
        current = (chunk.doc_id, tuple(chunk.section_path))
        if current != key:
            groups.append([])
            key = current
        groups[-1].append(chunk.source_text)
    return groups


def late_vectors(chunks: Sequence[Chunk], client: LateChunkingClient) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    for request in pack_groups(section_groups(chunks)):
        for text, vector in zip(request, client.embed(request), strict=True):
            vectors[text] = vector
    return vectors


def context_evidence(outcome: RetrievalOutcome) -> list[AnswerEvidence]:
    """Mirrors `rag_ingestion.api.deps.context_evidence` (that module cannot be
    imported without starting the web app)."""
    chunks = outcome.context_chunks or outcome.chunks
    return [
        AnswerEvidence(
            chunk_id=chunk.chunk_id,
            source_text=chunk.source_text,
            breadcrumb=chunk.index_text.split("\n", 1)[0],
        )
        for chunk in chunks
    ]


def retrieval_row(item: RequestaItem, outcome: RetrievalOutcome) -> dict:
    flags = [is_relevant(chunk.source_text, item.gold_passage) for chunk in outcome.chunks]
    evidence = context_evidence(outcome)
    return {
        "qid": item.qid,
        "hit": hit_at(flags, settings.HIT_AT),
        "mrr": mrr_at(flags, settings.MRR_AT),
        "context_tokens": sum(token_count(e.source_text) for e in evidence),
    }


def build_chunks(config_id: str, parsed: dict[str, ParseResult], bundle, runtime: RuntimeConfig):
    """Chunks and the embedder that indexes them, for one configuration."""
    embedder = bundle.embedder
    if config_id == "C1":
        chunks = [c for p in parsed.values() for c in chunk_fixed(p)]
    elif config_id == "C2":
        embed = lambda texts: embedder.embed_documents(texts, bundle.profile, intent="document")  # noqa: E731
        chunks = [c for p in parsed.values() for c in chunk_semantic(p, embed)]
    elif config_id in ("C3", "C5"):
        chunks = [c for p in parsed.values() for c in chunk_structure(p)]
    elif config_id == "C4":
        chunks = without_prefix([c for p in parsed.values() for c in chunk_structure(p)])
        client = LateChunkingClient(runtime.providers.jina_api_keys, bundle.profile)
        embedder = PrecomputedEmbedder(late_vectors(chunks, client), bundle.embedder)
    else:
        raise ValueError(config_id)
    return chunks, embedder


def run(config_ids: Sequence[str], model_keys: Sequence[str], limit: int | None, judge: bool) -> None:
    runtime = RuntimeConfig.from_environment()
    bundle = jina_bundle(runtime)
    items = load_requesta()[:limit] if limit else load_requesta()
    parsed = {
        doc: process_document(pdf_for(doc), owner_user_id=OWNER, project_id=PROJECT)
        for doc in sorted({item.document for item in items})
    }
    models = [m for m in settings.ANSWER_MODELS if any(key in m.model_id for key in model_keys)]
    judged = set(sample([item.qid for item in load_requesta()]))
    judge_client = Judge() if judge else None
    clients = {model.model_id: ChatClient(model.model_id, model.router) for model in models}

    for config_id in config_ids:
        store = ResultStore(settings.RESULTS_DIR / "e1" / f"{config_id}.jsonl")
        chunks, embedder = build_chunks(config_id, parsed, bundle, runtime)
        index = QdrantHybridIndex(QdrantClient(location=":memory:"), f"e1-{config_id.lower()}", bundle.profile)
        index.ensure_collection()
        index_corpus(index, chunks, embedder, quiet=True)
        store.put(("chunks",), {"kind": "chunks", "count": sum(c.level is ChunkLevel.LEAF for c in chunks)})
        retrieval = RetrievalConfig(auto_merge=config_id in settings.AUTO_MERGE_CONFIGS)
        for item in items:
            outcome = run_retrieval(
                index, bundle.embedder, bundle.reranker, item.question,
                owner_user_id=OWNER, project_id=PROJECT,
                limit=settings.RETRIEVAL_LIMIT, config=retrieval,
            )
            if not store.has(("retrieval", item.qid)):
                store.put(("retrieval", item.qid), {"kind": "retrieval", **retrieval_row(item, outcome)})
            evidence = context_evidence(outcome)
            for model in models:
                key = ("answer", model.model_id, item.qid)
                if store.has(key):
                    continue
                reply = clients[model.model_id].complete(build_turns(item.question, evidence))
                answer = clean_answer(reply.text)
                row = {
                    "kind": "answer", "model": model.model_id, "qid": item.qid, "answer": answer,
                    "f1": f1_score(answer, item.answer), "bleu1": bleu1(answer, item.answer),
                    "prompt_tokens": reply.prompt_tokens, "seconds": reply.seconds,
                }
                if judge_client and item.qid in judged:
                    row["label"] = judge_client.e1(
                        question=item.question, reference=item.answer,
                        passage=item.gold_passage, answer=answer,
                    )
                store.put(key, row)
        print(config_id, summarise(store.rows()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E1 chunking on REQuestA")
    parser.add_argument("--configs", nargs="+", default=list(settings.E1_CONFIGS), choices=settings.E1_CONFIGS)
    parser.add_argument("--models", nargs="+", default=["nemotron", "hy4"])
    parser.add_argument("--limit", type=int, default=None, help="First N questions (smoke runs)")
    parser.add_argument("--no-judge", action="store_true")
    options = parser.parse_args(argv)
    run(options.configs, options.models, options.limit, judge=not options.no_judge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
