"""Run this project's retrieval chain against MTRAG and score it.

    python backend/scripts/benchmark/mtrag_run.py --domain fiqa --provider cohere \
        --variant lastturn rewrite --max-passages 5000

What is actually measured
-------------------------
MTRAG ships a fixed passage corpus and judges relevance at passage level, so
each passage is indexed as one citable leaf with `chunk_id == passage_id`.
That is what makes the qrels line up, and it is the same granularity the
published BM25 / BGE / Elser baselines were computed at.

The consequence is worth stating plainly: with no hierarchy in the corpus, Q0
routing finds no sections, Q4 auto-merge has no parents to promote to, and Q5
definition pinning has no glossary. This scores **Q1 hybrid retrieval plus Q3
reranking**, not the whole chain. Hierarchy has to be measured on documents
that have some, which MTRAG does not provide.

What it is good for
-------------------
The three query variants share one id per turn, so they run against identical
qrels:

    lastturn   the final user message alone      floor
    rewrite    a human-written standalone query  ceiling
    questions  every user turn concatenated      raw context

The gap between floor and ceiling is the value a query rewriter can add, and
`UnderstandMessage` can be dropped in as a fourth variant to see where it
lands between them.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qdrant_client import QdrantClient  # noqa: E402

from rag_ingestion.chunking import Chunk, ChunkLevel  # noqa: E402
from rag_ingestion.config import RuntimeConfig  # noqa: E402
from rag_ingestion.indexing.qdrant_index import (  # noqa: E402
    EmbeddingProfile,
    QdrantHybridIndex,
)
from rag_ingestion.providers.adapters import (  # noqa: E402
    CohereEmbedder,
    CohereReranker,
    JinaEmbedder,
    JinaReranker,
    VoyageEmbedder,
    VoyageReranker,
)
from rag_ingestion.providers.registry import (  # noqa: E402
    Provider,
    ProviderBundle,
    ProviderError,
)
from rag_ingestion.retrieval import RetrievalConfig, run_retrieval  # noqa: E402
from scripts.benchmark.mtrag_data import (  # noqa: E402
    DEFAULT_ROOT,
    DOMAINS,
    QUERY_VARIANTS,
    Passage,
    read_corpus,
    read_qrels,
    read_queries,
)
from scripts.benchmark.mtrag_metrics import DEFAULT_CUTOFFS, evaluate  # noqa: E402

#: Benchmark rows are tenant-scoped like everything else, under an owner that
#: cannot collide with a real user id.
BENCHMARK_OWNER = "mtrag-benchmark"

#: Bounded by the strictest provider, not by what Qdrant would accept. The
#: embedder adapters send whatever list they are handed as one request, and
#: Cohere rejects more than 96 texts per call ("total number of texts must be
#: at most 96"), verified against the live API.
_UPSERT_BATCH = 96

#: Rough size of one reranked candidate. MTRAG passages average about 800
#: characters, and the reranker reads every candidate in the pool.
_RERANK_TOKENS_PER_CANDIDATE = 200


def passages_to_chunks(
    passages: Iterable[Passage], *, project_id: str
) -> Iterator[Chunk]:
    """One MTRAG passage becomes one citable leaf.

    `chunk_id` is the passage id verbatim: the qrels reference passage ids, so
    anything else here scores every hit as a miss.
    """
    for passage in passages:
        text = passage.text.strip()
        if not text:
            # An empty body embeds to nothing useful and some providers reject
            # the request outright.
            continue
        title = passage.title.strip()
        yield Chunk(
            chunk_id=passage.passage_id,
            doc_id=passage.passage_id,
            owner_user_id=BENCHMARK_OWNER,
            project_id=project_id,
            level=ChunkLevel.LEAF,
            source_text=text,
            index_text=f"{title}\n{text}" if title else text,
            source_block_ids=[passage.passage_id],
            locators=[],
            confidence=1.0,
            citable=True,
        )


def sample_passages(
    passages: Sequence[Passage],
    *,
    relevant: set[str],
    limit: int | None,
    seed: int = 0,
) -> list[Passage]:
    """Trim the corpus while keeping every judged passage.

    Sampling makes the haystack smaller and therefore the task easier, so
    numbers from a sampled run are comparable across providers and variants but
    **not** against MTRAG's published baselines. Judged passages are always
    kept: dropping one makes its query unanswerable and depresses recall for a
    reason that has nothing to do with retrieval quality.
    """
    if limit is None or len(passages) <= limit:
        return list(passages)
    judged = [passage for passage in passages if passage.passage_id in relevant]
    rest = [passage for passage in passages if passage.passage_id not in relevant]
    room = max(limit - len(judged), 0)
    random.Random(seed).shuffle(rest)
    return judged + rest[:room]


def _keys_for(config: RuntimeConfig, provider: Provider) -> list[str]:
    settings = config.providers
    return {
        Provider.COHERE: settings.cohere_api_keys,
        Provider.VOYAGE: settings.voyage_api_keys,
        Provider.JINA: settings.jina_api_keys,
    }[provider]


def _bundle(config: RuntimeConfig, provider: Provider) -> ProviderBundle:
    """Build one provider's embedder and reranker, and nothing else.

    Deliberately not `rag_ingestion.api.app._bundles`: importing that module
    executes `app = create_runtime_app()` at import time, which stands up the
    whole FastAPI app, opens Postgres and creates tables. A benchmark needs an
    embedder, not a web application.
    """
    settings = config.providers
    specs = {
        Provider.COHERE: (
            settings.cohere_api_keys,
            settings.cohere_embedding_model,
            settings.cohere_rerank_model,
            settings.cohere_dimensions,
            CohereEmbedder,
            CohereReranker,
        ),
        Provider.VOYAGE: (
            settings.voyage_api_keys,
            settings.voyage_embedding_model,
            settings.voyage_rerank_model,
            settings.voyage_dimensions,
            VoyageEmbedder,
            VoyageReranker,
        ),
        Provider.JINA: (
            settings.jina_api_keys,
            settings.jina_embedding_model,
            settings.jina_rerank_model,
            settings.jina_dimensions,
            JinaEmbedder,
            JinaReranker,
        ),
    }
    keys, embed_model, rerank_model, dimensions, embedder_type, reranker_type = specs[
        provider
    ]
    if not keys:
        raise SystemExit(
            f"No credentials for {provider.value}. "
            f"Set {provider.value.upper()}_API_KEY."
        )
    profile = EmbeddingProfile(
        provider=provider.value,
        model=embed_model,
        version=settings.profile_version,
        dimensions=dimensions,
        task="retrieval",
        mode="naive",
    )
    return ProviderBundle(
        provider=provider,
        profile=profile,
        collection_name="unused-by-the-benchmark",
        embedder=embedder_type(keys, profile),
        reranker=reranker_type(keys, rerank_model),
    )


class TokenBudget:
    """Pace requests to stay under a provider's tokens-per-minute allowance.

    Embedding a full MTRAG domain is tens of millions of tokens, and the
    provider keys in use here are trial keys: Cohere answers with
    `429 trial token rate limit exceeded, limit is 100000 tokens per minute`
    after roughly ten batches sent back to back. Retrying alone would spend the
    whole run in backoff, so requests are spaced to fit the budget instead, and
    retry is kept for the overshoot.

    Token counts are estimated at four characters each. A real tokenizer would
    be more accurate but would mean pulling in a tokenizer per provider, and
    being slightly conservative is the safe direction to be wrong in.
    """

    def __init__(self, tokens_per_minute: int, *, sleep=time.sleep) -> None:
        self.tokens_per_minute = tokens_per_minute
        self._spent = 0
        self._window_started = time.monotonic()
        self._sleep = sleep

    @staticmethod
    def estimate(texts: Iterable[str]) -> int:
        return sum(len(text) for text in texts) // 4 + 1

    def take(self, tokens: int) -> float:
        """Block until `tokens` fit in the current minute. Returns seconds slept."""
        if self.tokens_per_minute <= 0:
            return 0.0
        elapsed = time.monotonic() - self._window_started
        if elapsed >= 60:
            self._spent = 0
            self._window_started = time.monotonic()
            elapsed = 0.0
        if self._spent + tokens > self.tokens_per_minute:
            pause = max(60 - elapsed, 0.0)
            self._sleep(pause)
            self._spent = 0
            self._window_started = time.monotonic()
            self._spent = tokens
            return pause
        self._spent += tokens
        return 0.0


def _upsert_with_retry(
    index: QdrantHybridIndex,
    batch: list[Chunk],
    embedder,
    *,
    attempts: int = 5,
    sleep=time.sleep,
) -> None:
    """Retry a batch through provider failures.

    The adapters deliberately drop the provider's response body, so a rate
    limit and a malformed request arrive here as the same exception. Retrying
    both is the pragmatic choice: a bounded number of attempts costs little on
    a genuine 400 and rescues the run on a 429.
    """
    _retry(lambda: index.upsert(batch, embedder), attempts=attempts, sleep=sleep)


def budget_for(
    *, tokens_per_minute: int, keys: int, sleep=time.sleep
) -> TokenBudget:
    """Scale the allowance by the size of the key pool.

    `KeyRotator` moves to the next key on every call, so N keys carry N times
    the per-key limit. Pacing to a single key's allowance with a pool of two
    would run the whole benchmark at half speed for no reason.

    Kept separate from `TokenBudget` itself: the class is about spending a
    budget, this is about working out how large it is.
    """
    if tokens_per_minute <= 0:
        return TokenBudget(0, sleep=sleep)
    return TokenBudget(tokens_per_minute * max(keys, 1), sleep=sleep)


def _retry(call, *, attempts: int = 5, sleep=time.sleep):
    """Bounded retry around one provider round trip.

    Querying spends tokens too: a query embedding plus a rerank of the whole
    candidate pool, which on a trial key is enough to cross the same
    per-minute ceiling that indexing does. The budget paces the common case
    and this catches the overshoot.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ProviderError:
            if attempt == attempts:
                raise
            sleep(min(2**attempt, 60))
    raise AssertionError("unreachable")


def index_corpus(
    index: QdrantHybridIndex,
    chunks: Iterable[Chunk],
    embedder,
    *,
    budget: TokenBudget | None = None,
    quiet: bool = False,
) -> int:
    batch: list[Chunk] = []
    total = 0
    waited = 0.0

    def flush(pending: list[Chunk]) -> float:
        if budget is not None:
            return budget.take(TokenBudget.estimate(c.index_text for c in pending))
        return 0.0

    for chunk in chunks:
        batch.append(chunk)
        if len(batch) >= _UPSERT_BATCH:
            waited += flush(batch)
            _upsert_with_retry(index, batch, embedder)
            total += len(batch)
            batch = []
            if not quiet:
                print(
                    f"\r  indexed {total:,} (paced {waited:.0f}s)", end="", flush=True
                )
    if batch:
        waited += flush(batch)
        _upsert_with_retry(index, batch, embedder)
        total += len(batch)
    if not quiet:
        print(f"\r  indexed {total:,} (paced {waited:.0f}s)")
    return total


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Score this project's retrieval chain on the MTRAG benchmark."
    )
    argument_parser.add_argument("--domain", choices=DOMAINS, required=True)
    argument_parser.add_argument(
        "--provider",
        choices=[provider.value for provider in Provider],
        required=True,
    )
    argument_parser.add_argument(
        "--variant",
        nargs="+",
        choices=QUERY_VARIANTS,
        default=["lastturn", "rewrite"],
        help="Query variants to score. Defaults to the floor and the ceiling.",
    )
    argument_parser.add_argument(
        "--max-passages",
        type=int,
        default=None,
        help="Sample the corpus. Judged passages are always kept; results stop "
        "being comparable to MTRAG's published baselines.",
    )
    argument_parser.add_argument(
        "--limit-queries",
        type=int,
        default=None,
        help="Score only the first N queries. For smoke runs.",
    )
    argument_parser.add_argument("--top-k", type=int, default=10)
    argument_parser.add_argument(
        "--tokens-per-minute",
        type=int,
        default=100_000,
        help="Pace embedding to this budget. The default matches the Cohere "
        "trial limit. Pass 0 to disable pacing on a paid key.",
    )
    argument_parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="Skip indexing and query whatever is already in the collection.",
    )
    argument_parser.add_argument(
        "--in-memory",
        action="store_true",
        help="Use an in-process Qdrant instead of a server. Needs no running "
        "service and starts from a clean index, at the cost of re-indexing on "
        "every invocation.",
    )
    argument_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    return argument_parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    root: Path = options.root
    domain: str = options.domain
    provider = Provider(options.provider)

    corpus_path = root / "corpora" / f"{domain}.jsonl.zip"
    if not corpus_path.exists():
        print(
            f"Missing {corpus_path}. Run: python -c "
            f'"from scripts.benchmark.mtrag_data import fetch; fetch()"',
            file=sys.stderr,
        )
        return 2

    qrels = read_qrels(root / domain / "qrels.tsv")
    relevant = {passage_id for judged in qrels.values() for passage_id in judged}

    config = RuntimeConfig.from_environment()
    bundle = _bundle(config, provider)
    project_id = f"mtrag-{domain}"
    # A collection of its own per domain and provider, so a benchmark run can
    # never touch a real project's vectors.
    collection = f"mtrag-{domain}-{provider.value}"
    if options.in_memory and options.reuse_index:
        print("--reuse-index does nothing in memory; every run reindexes.", file=sys.stderr)
    client = (
        QdrantClient(location=":memory:")
        if options.in_memory
        else QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key)
    )
    index = QdrantHybridIndex(client, collection, bundle.profile)

    key_count = len(_keys_for(config, provider))
    budget = budget_for(
        tokens_per_minute=options.tokens_per_minute, keys=key_count
    )
    retrieval_config = RetrievalConfig()

    print(
        f"MTRAG {domain} / {provider.value} / {key_count} key(s) "
        f"/ budget {budget.tokens_per_minute:,} tok/min"
    )
    if options.reuse_index:
        print("  reusing the existing collection")
    else:
        passages = sample_passages(
            list(read_corpus(corpus_path)),
            relevant=relevant,
            limit=options.max_passages,
        )
        print(f"  corpus: {len(passages):,} passages ({len(relevant):,} judged)")
        index.ensure_collection()
        index_corpus(
            index,
            passages_to_chunks(passages, project_id=project_id),
            bundle.embedder,
            budget=budget,
        )

    for variant in options.variant:
        queries = read_queries(root / domain / f"{variant}.jsonl")
        if options.limit_queries:
            queries = queries[: options.limit_queries]
        runs: dict[str, list[str]] = {}
        for position, query in enumerate(queries, start=1):
            # Charged before the call, not measured after: one query costs
            # its own embedding plus a rerank over the whole candidate pool.
            budget.take(
                TokenBudget.estimate([query.text])
                + retrieval_config.max_candidates * _RERANK_TOKENS_PER_CANDIDATE
            )
            outcome = _retry(
                lambda: run_retrieval(
                    index,
                    bundle.embedder,
                    bundle.reranker,
                    query.text,
                    owner_user_id=BENCHMARK_OWNER,
                    project_id=project_id,
                    limit=options.top_k,
                    config=retrieval_config,
                )
            )
            runs[query.query_id] = [chunk.chunk_id for chunk in outcome.chunks]
            print(f"\r  {variant}: {position}/{len(queries)}", end="", flush=True)
        print()
        scored = {
            query_id: ranked
            for query_id, ranked in runs.items()
            if query_id in qrels
        }
        subset = {qid: rel for qid, rel in qrels.items() if qid in scored}
        scores = evaluate(scored, subset)
        cells = "  ".join(
            f"{name}={scores[name]:.3f}"
            for k in DEFAULT_CUTOFFS
            for name in (f"recall@{k}", f"ndcg@{k}")
        )
        print(f"  {variant:10} n={scores['queries']:4}  {cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
