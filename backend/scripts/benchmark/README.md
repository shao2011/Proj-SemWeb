# Retrieval Benchmarks

Two runners, measuring different things. Pick by the question you are asking.

| Runner | Measures | Needs |
|---|---|---|
| [`mtrag_run.py`](#mtrag-multi-turn-retrieval) | **This project's own chain**: hybrid search plus reranking, on multi-turn conversations | provider API key |
| [`retrieval.py`](#mteb-embedding-retrieval) | A **third-party embedding model** in isolation | Torch, MTEB |

If the question is "is our retrieval any good" or "which provider should we
use", it is `mtrag_run.py`. `retrieval.py` scores an off-the-shelf encoder and
never touches our Qdrant search or reranker.

---

# MTRAG: multi-turn retrieval

[MTRAG](https://github.com/IBM/mt-rag-benchmark) (IBM, Apache-2.0, TACL) is a
multi-turn conversational RAG benchmark in BEIR format. It is used here because
its queries come in three variants that share one id per turn, so all three
score against identical qrels:

| Variant | What it is | Role |
|---|---|---|
| `lastturn` | the final user message alone | floor |
| `rewrite` | a human-written standalone query | ceiling |
| `questions` | every user turn concatenated | raw context |

The gap between the floor and the ceiling is the headroom a query rewriter has.
That is exactly what `UnderstandMessage` tries to capture, and this is how to
find out whether it does.

## Get the data

```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe -c "from scripts.benchmark.mtrag_data import fetch; fetch()"
```

About 103 MB into `backend/scripts/benchmark/data/`, which is git-ignored.
Downloads are skipped when the file is already present.

| Domain | Passages | Queries |
|---|---:|---:|
| `clapnq` | 183,408 | 208 |
| `cloud` | 72,442 | 188 |
| `fiqa` | 61,022 | 180 |
| `govt` | 49,607 | 201 |

## Run it

Start small. `--in-memory` needs no Qdrant server and starts from a clean index:

```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe backend/scripts/benchmark/mtrag_run.py `
    --domain fiqa --provider cohere --in-memory `
    --max-passages 2000 --limit-queries 20 --variant lastturn rewrite
```

A full domain against a running Qdrant:

```powershell
.\.venv\Scripts\python.exe backend/scripts/benchmark/mtrag_run.py `
    --domain fiqa --provider cohere --variant lastturn rewrite questions
```

## Read the numbers carefully

**`--max-passages` breaks comparability with published baselines.** Sampling
shrinks the haystack, so scores rise for a reason that has nothing to do with
retrieval quality. Judged passages are always kept, so recall stays meaningful,
and comparisons *between* providers and variants on the same sample are valid.
Comparisons against the table below are not.

MTRAG's published baselines, for orientation (full corpus, nDCG@10):

| Retriever | Last turn | Query rewrite |
|---|---|---|
| BM25 | 0.21 | 0.25 |
| BGE-base 1.5 | 0.30 | 0.38 |
| Elser | 0.49 | 0.54 |

**What this does and does not exercise.** MTRAG judges relevance at passage
level, so each passage is indexed as one citable leaf and `chunk_id` is the
passage id. With no hierarchy in the corpus, Q0 routing finds no sections, Q4
auto-merge has no parents, and Q5 definition pinning has no glossary. This
scores **Q1 hybrid retrieval plus Q3 reranking**. Hierarchical chunking has to
be measured on documents that have a hierarchy.

## Rate limits

Trial provider keys are the binding constraint, not compute. Cohere answers
`429 trial token rate limit exceeded, limit is 100000 tokens per minute`, which
is about five batches sent back to back.

**More keys means a proportionally larger budget.** `KeyRotator` moves to the
next key on every call, so `COHERE_API_KEY=key-a,key-b` carries twice the
allowance of one key. The runner reads the pool size and scales
`--tokens-per-minute` (per key, default 100,000) by it, then retries with
backoff on whatever still slips through. The header line reports what it
worked out:

```
MTRAG fiqa / cohere / 2 key(s) / budget 200,000 tok/min
```

Pass `--tokens-per-minute 0` to disable pacing on a paid key. The
`indexed N (paced Ns)` counter shows how much of the wall clock went to
waiting rather than working.

Rough cost of a **full** domain, no sampling, at 200,000 tokens/minute:

| | Tokens | Time |
|---|---:|---:|
| fiqa corpus (61,022 passages) | 11.5M | ~57 min |
| 180 queries × 2 variants | 4.3M | ~22 min |
| **total** | **15.8M** | **~79 min** |

Adding keys is the only lever that shortens this materially.

---

# MTEB: embedding retrieval

This runner measures dense embedding retrieval with MTEB's standard metrics and
result format. It does not exercise the application's Qdrant hybrid search or
reranker.

## Datasets

| CLI task | MTEB task | Language |
|---|---|---|
| `mteb/AILA_casedocs` | `AILACasedocs` | English |
| `mteb/AILA_statutes` | `AILAStatutes` | English |
| `mteb/LegalQuAD` | `LegalQuAD` | German |
| `mteb/legal_summarization` | `LegalSummarization` | English |
| `mteb/miracl-hard-negatives` | `MIRACLRetrievalHardNegatives` | 18 languages |

`legal_summarization` is exposed by MTEB as document retrieval: summaries are
queries and their contracts are relevant documents. This runner does not
generate summaries.

## Install

The project environment is managed by `uv` and pinned to Python 3.12. From the
repository root, install the application and test dependencies with:

```powershell
uv sync --extra test
```

This default environment does not install the `benchmark` extra or Torch. Torch
may appear in `uv.lock` because the lock records optional dependencies, but it
is not installed by the command above.

MTEB requires Torch. Before running a benchmark, each developer must install
the Torch build appropriate for their CPU or CUDA environment. For example,
the CPU-only build on Windows is:

```powershell
uv pip install torch --torch-backend cpu
```

Use the command generated by the official PyTorch installer selector when a
CUDA build is required. After Torch is installed, synchronize the locked
benchmark tooling without replacing the developer-managed Torch package:

```powershell
uv sync --extra test --extra benchmark --no-install-package torch --inexact
```

`--no-install-package torch` prevents the project lock from choosing a Torch
build. `--inexact` preserves the manually installed package while the remaining
benchmark dependencies still come from `uv.lock`. Install Torch first: the
project deliberately does not choose a CPU or CUDA build for developers. The
benchmark extra is separate from production dependencies because MTEB also
installs model and dataset tooling.

## First Run

Start with one small English task on CPU. This verifies model loading, dataset
download, evaluation, and result writing without starting MIRACL:

```powershell
.\.venv\Scripts\python.exe backend/scripts/benchmark/retrieval.py `
	--model sentence-transformers/all-MiniLM-L6-v2 `
	--task mteb/AILA_statutes `
	--device cpu `
	--output backend/scripts/benchmark/results
```

Run the four legal datasets with a multilingual model:

```powershell
.\.venv\Scripts\python.exe backend/scripts/benchmark/retrieval.py `
	--model intfloat/multilingual-e5-small `
	--task mteb/AILA_casedocs mteb/AILA_statutes mteb/LegalQuAD mteb/legal_summarization `
	--device cuda `
	--batch-size 64
```

Run English and German MIRACL hard negatives explicitly:

```powershell
.\.venv\Scripts\python.exe backend/scripts/benchmark/retrieval.py `
	--model intfloat/multilingual-e5-small `
	--task mteb/miracl-hard-negatives `
	--miracl-language eng deu `
	--device cuda
```

MIRACL hard negatives span about 2.45 million candidate documents across all 18
languages. The runner defaults to English and never selects all languages
implicitly. Add languages only when their download and inference cost is
intentional.

Use `--help` for the full language list and options. Omitting `--task` selects
all four legal tasks plus English MIRACL.

## Results

Results are written through `mteb.ResultCache` under the `--output` directory.
MTEB records model and task metadata with standard retrieval metrics such as
nDCG, MAP, recall, precision, and MRR at its configured cutoffs. The default
`backend/scripts/benchmark/results/` directory is ignored by Git.
