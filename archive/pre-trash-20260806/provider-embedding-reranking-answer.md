---
title: 'Provider-selectable embedding, reranking, and mock answers'
type: 'feature'
created: '2026-08-02'
status: 'done'
baseline_commit: 'NO_VCS'
context:
  - 'document/rag-workflow/1.ingestion.md'
---

<frozen-after-approval reason="human-owned intent, do not modify unless human renegotiates">

## Intent

**Problem:** The API currently uses a local hash embedder, has no reranking stage, and returns retrieval evidence without an answer. It also has no private project boundary, so a user cannot group multiple files and query only that project's corpus.

**Approach:** Add private user-owned projects that pin one embedding provider/profile, attach every document and chunk to a user and project, and add provider-specific HTTP adapters behind common embedding and reranking contracts. Keep Qdrant collections profile-based, authorize project ownership before every operation, then isolate retrieval with mandatory owner and project payload filters.

## Boundaries & Constraints

**Always:** Accept only `cohere`, `voyage`, or `jina`; pin a provider/profile when a project is created; persist project and file metadata outside Qdrant; require ownership before ingest, list, query, and answer; use a separate Qdrant collection per embedding profile; store `owner_user_id` and `project_id` on documents and chunk payloads; create payload indexes for both; filter by `owner_user_id`, `project_id`, and `citable`; map each vendor's native contract; retrieve more candidates than the final limit; rerank before returning evidence; return citation provenance with mock answers; sanitize provider failures.

**Ask First:** Changing the selected default model, dimensions, collection naming rule, public authentication scheme, or replacing the mock answerer with a paid LLM.

**Never:** Create one Qdrant collection per project; mix vectors from different providers or profiles in one collection; let a request override its project's pinned provider; trust a project ID without checking its owner; rely on `project_id` as the only Qdrant isolation filter; store sharing ACLs in Qdrant; silently fall back to local embeddings; call external providers in unit tests; expose secrets.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Create project | Name, provider, and authenticated user | Persist private user-owned project with pinned embedding profile | Invalid provider returns 422 |
| Ingest | Multipart document, project, and user | Verify ownership, parse, chunk, embed with project provider, upsert profile collection | Missing or foreign project returns 404; provider failure returns 503 |
| List project files | Project and user | Return owned file metadata and processing status without scanning Qdrant | Foreign or missing project returns 404 |
| Query | Query, project, user, limit | Verify ownership, retrieve only owner/project chunks, provider rerank, return final hits | Missing collection returns 503; empty evidence returns 200 with no hits |
| Answer | Question, project, user, limit | Same project-scoped retrieve/rerank path, deterministic mock answer plus citations | No evidence returns explicit mock no-evidence answer |
| Foreign project ID | User requests another user's project | No project existence or evidence is disclosed | Return 404 |
| Profile mismatch | Existing provider collection has another profile | Reject request without modifying collection | Return 503 configuration error |

</frozen-after-approval>

## Code Map

- `backend/rag_ingestion/providers.py` -- provider enum, bundles, registry, normalized provider errors.
- `backend/rag_ingestion/provider_adapters.py` -- Cohere, Voyage, and Jina HTTP embedding/reranking adapters.
- `backend/rag_ingestion/reranking.py` -- provider-neutral reranker contract and ranked result model.
- `backend/rag_ingestion/answering.py` -- answerer contract and deterministic mock implementation.
- `backend/rag_ingestion/projects.py` -- project and file metadata models, store contract, and local persistence implementation.
- `backend/rag_ingestion/models.py`, `chunking.py` -- propagate project identity from document to every chunk.
- `backend/rag_ingestion/query_api.py` -- provider-aware process/query/answer routes and shared retrieval pipeline.
- `backend/rag_ingestion/qdrant_index.py` -- owner/project payload indexes and mandatory isolation filters.
- `backend/rag_ingestion/app.py` -- environment-backed keys, model profiles, provider collections, and registry bootstrap.
- `compose.yaml` -- provider environment variables passed into the API container.
- `tests/` -- mocked vendor contracts, registry isolation, API reranking, and mock answer behavior.

## Tasks & Acceptance

**Execution:**
- [x] `backend/rag_ingestion/reranking.py`, `answering.py`, `providers.py` -- define strict provider-neutral contracts and registry lookup.
- [x] `backend/rag_ingestion/projects.py` -- persist private user-owned projects and file records with pinned profile, processing status, content hash, and owner-scoped create/get/list boundaries.
- [x] `backend/rag_ingestion/models.py`, `chunking.py`, `qdrant_index.py` -- propagate owner/project identity, index both payload fields, and require both filters.
- [x] `backend/rag_ingestion/provider_adapters.py` -- implement authenticated HTTPX adapters with vendor-specific intent, batching, response parsing, dimensions, rerank fields, timeouts, and normalized errors.
- [x] `backend/rag_ingestion/query_api.py` -- add project creation; require `project_id` in process/query/answer; resolve the pinned provider; share expanded retrieval plus reranking; add `POST /answer` with mock output and citations.
- [x] `backend/rag_ingestion/app.py`, `compose.yaml`, package exports -- build three provider bundles from environment without requiring keys at startup.
- [x] `tests/` -- add RED contract tests first for all vendor payloads, private ownership, project isolation, citations, and upload flows.
- [x] `CHANGELOG.md`, `IMPLEMENT.md` -- record API contract, configuration, and verification results.

**Acceptance Criteria:**
- Given each provider adapter, when embedding query versus documents, then its exact native intent and response mapping are used.
- Given a project with multiple files, when they are ingested, then every chunk carries the same owner/project identity and all files are searchable together.
- Given a project file, when processing succeeds or fails, then its metadata registry records the terminal status without relying on a Qdrant scan.
- Given a user requests another user's project ID, when any project operation runs, then it returns 404 before Qdrant is queried.
- Given multiple users and projects in one profile collection, when one is queried, then only points matching both owner and project can be retrieved.
- Given a project pinned to one provider, when ingest/query/answer runs, then the project provider is used and cannot be overridden by request input.
- Given a candidate pool, when `/query` runs, then reranker order and score determine the returned limited hits.
- Given `/answer`, when evidence exists, then the mock answer and citation list use only reranked project-filtered citable evidence.
- Given two providers, when they ingest the same document, then points are stored in different profile-compatible collections.
- Given absent credentials, when that provider is requested, then the API returns a sanitized 503 and the app remains healthy enough to serve docs.

## Design Notes

Use collection names `<base>-<provider>-<profile-version>` and immutable profiles. Projects are logical partitions inside those collections, not physical collections. A project is private and stores `owner_user_id`; Qdrant duplicates that stable owner identity only as a defense-in-depth filter, while authorization remains in the metadata store. During local development `X-User-ID` supplies identity; later authentication derives it from an access token. A project pins its provider because changing provider requires re-embedding its files. Recommended defaults: Cohere `embed-v4.0`/`rerank-v4.0-pro`; Voyage `voyage-4`/`rerank-2.5`; Jina `jina-embeddings-v4`/`jina-reranker-v3`, all at 1024 dimensions.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/test_provider_adapters.py tests/test_query_api.py tests/test_process_api.py tests/test_app.py -q` -- provider and API contracts pass.
- `.venv/Scripts/python.exe -m pytest --cov=rag_ingestion --cov-report=term-missing --cov-fail-under=80 -q` -- full suite passes with at least 80% coverage.
- `docker compose up --build -d` then request `/openapi.json` -- project routes and `/answer` exist, while process/query/answer are project-scoped.

## Suggested Review Order

**Project API Boundary**

- Start with ownership-first routing, pinned-provider resolution, reranking, and mock answers.
  [`api/routes.py`](../backend/rag_ingestion/api/routes.py)

- Inspect expanded retrieval and mandatory owner/project query context.
  [`api/routes.py`](../backend/rag_ingestion/api/routes.py)

- Review terminal file status across parse, provider, and index failures.
  [`api/routes.py`](../backend/rag_ingestion/api/routes.py)

**Persistence And Isolation**

- Review private project persistence, profile pinning, and file status transitions.
  [`storage/projects.py`](../backend/rag_ingestion/storage/projects.py)

- Verify immutable collection profiles and owner/project payload indexes.
  [`indexing/qdrant_index.py`](../backend/rag_ingestion/indexing/qdrant_index.py)

**Provider Integration**

- Follow the normalized registry and missing-credential boundary.
  [`providers/registry.py`](../backend/rag_ingestion/providers/registry.py)

- Inspect native Cohere mapping; Voyage and Jina follow the same contract shape.
  [`providers/adapters.py`](../backend/rag_ingestion/providers/adapters.py)

- Review environment-backed bundles and startup without credentials.
  [`api/app.py`](../backend/rag_ingestion/api/app.py)

**Verification**

- Confirm foreign ownership returns 404 before any Qdrant access.
  [`test_project_api.py:138`](../tests/test_project_api.py#L138)

- Check exact mocked embedding intent and response mappings for all vendors.
  [`test_provider_adapters.py:80`](../tests/test_provider_adapters.py#L80)

- Review Docker credential passthrough and persistent metadata volume.
  [`compose.yaml:1`](../compose.yaml#L1)
