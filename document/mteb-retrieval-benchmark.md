---
title: 'MTEB retrieval benchmark runner'
type: 'feature'
created: '2026-08-06'
status: 'done'
baseline_commit: 'e793d3e'
context:
  - 'document/rag-workflow/1.ingestion.md'
---

<frozen-after-approval reason="human-owned intent - do not modify unless human renegotiates">

## Intent

**Problem:** The repository has no repeatable retrieval benchmark. The empty `backend/scripts/benchmark/` directory cannot currently run or compare the requested MTEB legal and multilingual retrieval tasks.

**Approach:** Add an optional benchmark dependency and a small Python CLI around MTEB 2.x. It selects the five requested tasks, scopes MIRACL languages explicitly, loads a caller-selected embedding model, and writes standard MTEB result JSON to a caller-visible directory.

## Boundaries & Constraints

**Always:** Use the official MTEB task names `AILACasedocs`, `AILAStatutes`, `LegalQuAD`, `LegalSummarization`, and `MIRACLRetrievalHardNegatives`; require an explicit model; keep MIRACL language selection separate from the fixed-language legal tasks; use ISO 639-3 codes; expose batch size and device; preserve MTEB's standard metrics and result format; unit-test orchestration without downloading datasets or models.

**Ask First:** Adding API-provider adapters, custom metrics, prompts, reranking, Qdrant, hybrid retrieval, result aggregation, or running a paid/large benchmark.

**Never:** Treat `legal_summarization` as generation; silently evaluate all 18 MIRACL languages; hardcode one model as the benchmark truth; import benchmark dependencies from production application code; commit downloaded datasets, model weights, predictions, or result artifacts.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|---------------------------|----------------|
| Default suite | Model supplied, no task override | Select all four legal tasks plus English MIRACL | Standard MTEB JSON is written under the output directory |
| Task subset | One or more supported task aliases | Run only those tasks in stable CLI order | Unknown task is rejected by argparse before model loading |
| MIRACL subset | MIRACL selected with one or more ISO 639-3 codes | Build only those MIRACL language subsets | Invalid language is rejected locally with the supported list |
| No MIRACL | Legal tasks only plus MIRACL language flags | Ignore MIRACL language selection because no multilingual task is built | No dataset or model is loaded during argument validation |
| Dependency absent | CLI invoked without benchmark extra installed | Exit with an install command that names the benchmark extra | Do not emit a Python traceback for this expected setup error |

</frozen-after-approval>

## Code Map

- `backend/scripts/benchmark/retrieval.py` -- dependency-light argument parsing, task registry, MTEB orchestration, and CLI entry point.
- `backend/scripts/benchmark/README.md` -- installation, task mapping, safe first run, MIRACL size warning, outputs, and examples.
- `backend/tests/test_retrieval_benchmark.py` -- mocked MTEB contract tests with no network or model loading.
- `pyproject.toml` -- isolated `benchmark` optional dependency.
- `.gitignore` -- excludes generated benchmark result and prediction directories.

## Tasks & Acceptance

**Execution:**
- [x] `backend/tests/test_retrieval_benchmark.py` -- add RED tests for task aliases, MIRACL language scoping, MTEB calls, invalid input, and missing dependency behavior.
- [x] `backend/scripts/benchmark/retrieval.py` -- implement the validated CLI and inject the MTEB module at the orchestration boundary for cheap tests.
- [x] `pyproject.toml` -- add `benchmark = ["mteb>=2.18,<3"]` without adding it to production dependencies.
- [x] `backend/scripts/benchmark/README.md` -- document install and commands, including a one-task CPU smoke run before MIRACL.
- [x] `.gitignore`, `CHANGELOG.md`, `IMPLEMENT.md` -- keep generated artifacts out of Git and record the completed feature.

**Acceptance Criteria:**
- Given a valid model and default arguments, when the runner starts, then it passes exactly five retrieval task objects to `mteb.evaluate`, with MIRACL limited to English.
- Given a supported task subset and MIRACL languages, when task selection runs, then only the selected task objects and requested MIRACL subsets are built.
- Given tests run in the normal test environment, when benchmark dependencies and network are unavailable, then all runner tests still pass through fakes.
- Given the benchmark extra is installed, when `--help` is invoked, then it succeeds without downloading a dataset or model.

## Spec Change Log

## Design Notes

The CLI uses stable local aliases matching Hugging Face repository names, while translating them to MTEB task names internally. MIRACL defaults to `eng` because its hard-negative task contains roughly 2.45 million documents across 18 languages; requiring deliberate expansion prevents an unexpectedly large first run. Standard MTEB output remains the source of truth so results stay compatible with MTEB tooling and leaderboards.

### Datasets not wired up yet

- **BRIGHT** (<https://huggingface.co/datasets/xlangai/BRIGHT>) — reasoning-intensive retrieval, where the relevant document does not share surface vocabulary with the query. Closer to this product's real workload than the lexical-overlap benchmarks above, so it is the first candidate to add. Kept as a pointer rather than a task alias because nothing in the runner references it yet.

## Verification

**Commands:**
- `.venv\Scripts\python.exe -m pytest backend/tests/test_retrieval_benchmark.py -q` -- focused orchestration tests pass without network access.
- `.venv\Scripts\python.exe -m pytest --cov=rag_ingestion --cov-report=term-missing --cov-fail-under=80 -q` -- existing backend suite and coverage gate remain green.
- `.venv\Scripts\python.exe backend/scripts/benchmark/retrieval.py --help` -- CLI help exits successfully without loading MTEB.
- `.venv\Scripts\python.exe -m pip install -e ".[benchmark]"` followed by one small legal task run -- installation and real MTEB integration succeed before any MIRACL run.

## Suggested Review Order

**Execution Boundary**

- Entry point validates arguments before loading the optional MTEB dependency.
  [`retrieval.py:175`](../backend/scripts/benchmark/retrieval.py#L175)

- Orchestration preserves MTEB's model, cache, task, and metric contracts.
  [`retrieval.py:157`](../backend/scripts/benchmark/retrieval.py#L157)

**Task and Language Selection**

- Registry maps requested Hugging Face datasets to official MTEB task names.
  [`retrieval.py:19`](../backend/scripts/benchmark/retrieval.py#L19)

- MIRACL alone receives explicit language subsets; legal task languages stay fixed.
  [`retrieval.py:130`](../backend/scripts/benchmark/retrieval.py#L130)

- CLI limits aliases, languages, and batch size before model loading.
  [`retrieval.py:74`](../backend/scripts/benchmark/retrieval.py#L74)

**Verification**

- Default-suite test pins five tasks and English-only MIRACL.
  [`test_retrieval_benchmark.py:35`](../backend/tests/test_retrieval_benchmark.py#L35)

- Orchestration test pins the exact MTEB API call shape.
  [`test_retrieval_benchmark.py:130`](../backend/tests/test_retrieval_benchmark.py#L130)

- Missing dependency and help tests protect the dependency-light CLI boundary.
  [`test_retrieval_benchmark.py:164`](../backend/tests/test_retrieval_benchmark.py#L164)

**Installation and Operation**

- Optional extra keeps heavy benchmark tooling out of production dependencies.
  [`pyproject.toml:25`](../pyproject.toml#L25)

- Runbook starts small and warns before expanding MIRACL.
  [`README.md:32`](../backend/scripts/benchmark/README.md#L32)
