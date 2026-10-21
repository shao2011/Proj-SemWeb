# Implementation Log

## PPTX via document conversion (complete 2026-08-20)

Discussed: the native reader was landing badly on real decks -- "kết quả cho pptx quá tệ, không xử lý được ảnh". A geometric prototype in `.temp/pptx_parser.py` recovered box grouping and arrow flows from the shape tree, but could never touch a slide whose content is a picture, which is where the value was.

Proven before committing to it, on the user's own decks:
- `easy.pptx` draws a spiral scatter plot with 83 shapes and embeds no image at all. The service rendered it, returned the picture, and described its content.
- The 18-slide technical deck: 16s, one page per slide, and full descriptions of the three AWS architecture diagrams naming `incomingA`, `receiveDocument`, `ocr-reporter-bus`, `SSM`, `Cognito User Pool` -- all previously invisible.
- Cost is 1 cent per page, and image captions add nothing to it.

User decisions: on by default for pptx; no data-residency constraint, so no self-hosted renderer; docx and xlsx stay on their existing parsers.

Rejected on evidence: converting locally with LibreOffice. Measured at +722MB on the API image (411MB minimal, 501MB with the CJK fonts this deck needs), against zero for letting the service do it.

### Implemented

- `parsers/datalab.py` -- `ConvertedBlock` (text, page, bbox, type, media, furniture), the `DocumentConverter` protocol, and `DatalabClient.convert`. Pages are converted from the response's zero-based numbering to one-based here, once, so a citation cannot land a slide off. Each block carries its own picture inline as base64, so nothing is matched up against a document-level list.
- `parsers/office.py` -- `parse_pptx` takes a converter and uses it when present, falling back to the shape tree on failure or an empty result. Repeated-figure and furniture filtering live here.
- `markdown.py` -- a figure renders as picture then caption.
- `pipeline.py`, `api/deps.py`, `api/routes.py`, `api/app.py`, `api/routers/projects.py` -- the converter is threaded through. `create_query_app` takes its own `converter` parameter rather than reusing `scan_parser`, so a test's fake OCR client is never mistaken for one.
- 14 tests in `tests/test_pptx_conversion.py`.

Status: complete. 891 backend tests pass.

**Two tests fail and are not from this work:** `test_process_api.py::test_batch_upload_processes_files_concurrently_and_streams_results` and `::test_batch_upload_failure_does_not_block_other_files`, both `Invalid file status transition: pending -> completed` under concurrent upload. Confirmed against a clean worktree at HEAD, where all 17 pass, so they come from the uncommitted admin/moderation work in the tree.

**Known rough edge:** two decorative arrows on one slide are captioned `Arrow pointing right` and survive, because they repeat only twice and the threshold is three.

## R2 object storage for media (complete 2026-08-18)

Discussed: the user added R2 credentials to `.env` and asked to test the connection, then to wire it up. This closes the item the Supabase round left open below.

Connection was tested before any code was written, with `curl --aws-sigv4` rather than by installing a client first. Worth keeping: the first token was **Object Read only**. `LIST` returned 200 and `PUT`/`DELETE` returned 403 `AccessDenied`. That is a permissions answer, not a signing one, because a bad signature comes back as `SignatureDoesNotMatch`, and because `DELETE` signs exactly like the `GET` that worked. The user reissued the token with Object Read & Write and all five probes passed.

### Decisions

**Proxy, not presigned URLs.** This was listed as an open question. Reading the code settled it: `GET /projects/{project_id}/media/{key}` already fetches the bytes and returns them behind an ownership check, and its key validation was already written for an object-storage backend. Presigned URLs would have meant a second authorization path for the same bytes, so the route is untouched.

**`boto3` as a normal dependency, not an extra.** An optional extra means a deploy can install the package set that cannot reach its own storage. It costs everyone about 15 MB.

**All four settings or none, enforced in config.** Falling back to local disk on a partial config is the failure that looks like success: uploads land on the container's disk and disappear with it. `RuntimeConfig.validate_media_backend` names the missing variables and the API does not start.

**A separate module rather than more of `media.py`.** `storage/r2_media.py` next to `storage/media.py`, matching the `projects.py` / `pg_projects.py` split the package already uses for a contract and its backing.

### What was built

- `storage/r2_media.py`: `R2MediaStore` implementing the four `MediaStore` methods, `from_credentials` pinning `region_name="auto"` and s3v4, and `build_media_store(config)` which returns the store plus a label for the startup log.
- `config.py`: four settings, an `r2_credentials` property, and the all-or-none validator.
- `api/app.py`: one call to `build_media_store`. The startup log line now reports `r2:<bucket>` or the local path.
- `storage/__init__.py`: exports `R2MediaStore` and `build_media_store`.
- `compose.yaml`: the four settings are handed to the api container. It never received them, so the container was the one place where configuring R2 would have had no effect at all.
- `tests/test_r2_media.py`: 23 tests on a fake S3 client.

### Verified

21 unit tests, and a live round trip against the real bucket: put, get, exists on both a present and a missing key, a missing key raising `FileNotFoundError`, a rejected traversal key, prefix delete, and a sibling document whose id shares the prefix surviving it. Probes deleted, bucket back to zero keys.

Three tests fail in the working tree and none of them are from this work: `test_app.py` expects a path set that the in-flight admin routes are not in, and two batch-upload tests in `test_process_api.py` fail on a `pending -> completed` status transition. Proved by applying only these changes on top of `HEAD` in a scratch worktree, where all three pass.

### Still open

- `document/techstack.md` still describes a stage-1 stack that does not match what is built.
- Existing documents indexed while media was on local disk are not migrated. Their keys still resolve against `MEDIA_ROOT`, so switching a running deployment to R2 makes their images and markdown 404 until they are re-uploaded or the files are copied into the bucket.

## Bookkeeping follows the session's model; default is DeepSeek V4 Flash (complete 2026-08-24)

Discussed: the user resolved the pending "part 1" the other way -- instead of pinning `UNDERSTAND_MODEL`/`SUMMARY_MODEL`, understanding and summarization should run on whatever model the user picked for the session. And the default model becomes `deepseek/deepseek-v4-flash-free` on OrcaRouter.

### Decisions

**Model/router ride the call, not the constructor.** `LLMUnderstander` and `LLMSummarizer` keep their own `BaseAgent`s (with the env models as fallback) and gain per-call `model`/`router`, mirroring how generation already worked. The turn's resolved spec is the single source: `run_understanding` passes it, `preflight_turns` hands it to `fold_through`, and the compact route lets `fold_through` resolve `thread.last_model_id` itself (the turn's pick is not stored yet mid-turn, so the in-flight spec must be passed explicitly there).

**No pick means the catalog default now.** The old rule -- "no pick leaves the provider on its own model" -- could not survive multi-router: the env model is an id on the default router, and with the catalog default on OrcaRouter an unpicked turn would have been sent to the wrong gateway. `resolve(request.model_id)` (None -> catalog default) replaces `resolve(request.model_id or completer.model)`, and every generation call passes `model=spec.id, router=spec.router` unconditionally. `model_used`, the prompt-metrics row, the `done` event and the debug log all report the same spec.

**What deliberately did not move:** the project `/answer` route (LLMAnswerer passes no per-call model, so it stays on `OPENROUTER_MODEL` @ the default router), and the token caps -- see evidence below.

### Token-cap evidence (why the caps stayed)

The standing worry was that reasoning models burn `max_completion_tokens` on thinking before emitting JSON, and understand/summary would silently degrade at 256/400. Measured live against the new default: the real `UNDERSTAND_SYSTEM_PROMPT` on `deepseek/deepseek-v4-flash-free` via OrcaRouter returned clean parseable JSON (29 chars) at cap 256 and identically at 1024. No reasoning burn on this model over the OpenAI-compatible path, so no cap change was made. If the picker later gains a model that does burn the cap, the failure signature is `UnderstandMessage failed`/`Fact extraction failed` warnings in the logs.

### Implemented

- `understanding.py`, `summary/summarizer.py`: protocol + implementation kwargs; both summarizer phases (extract, curate) carry them.
- `api/routers/threads.py`: resolution change, unconditional `model`/`router` on generation, spec passed to understanding; debug log reports `model_used`.
- `api/deps.py`: `fold_through(..., spec=None)` resolves from the thread when not handed one; `preflight_turns` passes its spec.
- `data/models.json`: default -> `deepseek/deepseek-v4-flash-free`.
- Tests: pass-through units for understander and both summarizer phases; API tests for no-pick-lands-on-default and fold-billed-to-picked-model; `_keyed_llm` in `test_app.py` keys both routers (boot refuses a keyless catalog-default router); two no-pick assertions updated to the new semantics; remaining fakes gained the kwargs.

### Status

Complete and verified. Full suite: **861 passed** (mtrag included this run, 3m51s). Live: understand on the new default model produced valid JSON at the configured caps; the previous session's smoke (one request per gateway, usage reported on Orca's stream) still stands.

## Multi-router LLM gateway: OrcaRouter alongside OpenRouter (complete 2026-08-24)

Discussed: the user asked whether adding OrcaRouter could be managed with a key in `models.json` ("router hay provider gì đó"), expecting `llm.py` to get simpler. Two prior decisions carried in: understand/summary model pinning stays pending (untouched), and a model whose router has no keys must degrade for the user (hidden from the picker, turn answered on the default) with only a dev-side log -- not a hard failure. The one agreed exception: boot raises when the catalog default itself is unreachable, because that is the model every degradation lands on.

### Decisions

**The key is named `router`, not `provider`.** `LLMConfig.provider` already means LangChain's `model_provider` (the wire format), and both gateways speak "openai". Two concepts, two names.

**`base_url` is stored whole, per router.** OpenRouter serves under `/api/v1`, OrcaRouter under `/v1`. The old code concatenated `/api/v1` in the app factory, which is exactly why a second gateway could not be configured. `OPENROUTER_BASE_URL` therefore changed meaning from host to full prefix -- checked `.env` before doing it: the variable was not set anywhere, so nothing breaks.

**Router resolution lives in `BaseAgent`, not `llm.py`.** `llm.py` changed by zero lines: it is a pure function of (provider, model, key, base_url, ...) and its `lru_cache` was already keyed by `base_url`, so two routers get two cached clients for free. `BaseAgent` now holds `{name: StickyKeyPool}` and `{name: base_url}`, resolves `router or default_router` per call. Sticky mapping is per pool, so a thread pins one key per gateway independently.

**Keyless router semantics, three layers.** Construction: allowed, unless it is the *default* router (then `ValueError` -- no call could ever succeed). Per call: `ProviderRequestError` -- by then the route handler has had its chance to fall back, so a miss is a wiring bug. API layer: `/models` hides those specs (`ApiContext.routers: frozenset | None`, `None` = unknown = no filtering so direct test construction keeps the full picker); a request that still names one degrades to the catalog default with a `_log.warning`, mirroring the existing unknown-id policy.

**Env models stay ids on the default router.** `OPENROUTER_MODEL` / `UNDERSTAND_MODEL` / `SUMMARY_MODEL` are documented as ids on `default_router`. Setting one to an OrcaRouter id is a misconfiguration the code does not chase (the per-request picker path is the supported way to reach the second gateway).

### Also fixed: the empty-directory Firebase credential

The whole local suite failed at collection: `eslothai-chat-firebase-adminsdk-fbsvc-c52b09b3fd.json` on disk is an empty *directory*, created by a `docker compose up` run while the real credential file was missing (compose bind-mounts it at `compose.yaml:134`; Docker fabricates absent bind sources as directories). The glob in `default_firebase_service_account_path` matched it, the verifier refused it, and `app.py`'s module-level `create_runtime_app()` turned that into an import error for every API test module. Fix: the glob now matches files only, restoring the documented "no credential -> auth off" behaviour. The stray directory itself was left in place (the running `rag-api` container references the mount); replacing the real credential file is the user's call.

### Implemented

- `schemas/model_catalog.py`: `router: str = "openrouter"` on `ModelSpec`.
- `config.py`: `RouterConfig` (`base_url`, `api_keys`), `LLMConfig.routers` + `default_router` + `configured_routers` property, validator that `default_router` is in the table; `from_env` builds both routers; Firebase glob fix.
- `chat.py`: `router` param on the `ChatCompleter` protocol.
- `core/agent.py`: per-router pools as above.
- `api/app.py`: no more URL concatenation; gates on `default_router in configured_routers`; boot check (raise on unreachable catalog default, warn-and-list for other hidden models); passes `configured_routers` to `create_query_app` (`None` when nothing has keys, so mock mode keeps the full picker).
- `api/routes.py` + `api/deps.py`: `configured_routers` threaded into `ApiContext.routers`.
- `api/routers/system.py`: picker filter. `api/routers/threads.py`: degradation branch + `router=` passed to `completer.stream`.
- `data/models.json`: `deepseek/deepseek-v4-pro-free` and `deepseek/deepseek-v4-flash-free` on `orcarouter`, conservative 128k/16k budgets (Orca's unpaid tier has an unpublished per-request prompt cap; revise upward after a top-up).
- `.env.example`: `ORCAROUTER_API_KEY`, commented full-URL examples for both `*_BASE_URL`.
- Tests: router-selection/keyless-router/keyless-default tests in `test_base_agent.py` (assert on `openai_api_base`, no network); router table + env parsing in `test_config.py`; `_keyed_llm` helper in `test_app.py`; picker filter, no-knowledge-no-filter, and degradation SSE test in `test_thread_api.py`; `router` default in `test_model_catalog.py`; fakes gained the `router` kwarg.

### Status

Complete and verified. Full suite: **816 passed** (3m45s) plus mtrag 35 passed = 851; baseline before the change was 56 passed + 1 environment failure (the Firebase directory, now fixed) with four suites uncollectable for the same reason. Live smoke, one real request per gateway through `BaseAgent`: OpenRouter `google/gemma-4-31b-it:free` -> "pong" (first attempt hit a transient free-tier failure, retry clean); OrcaRouter `deepseek/deepseek-v4-flash-free` -> "pong" with usage on the streamed response (89 prompt / 28 completion / 117 total), confirming `stream_usage` works on Orca and the accounting table fills.

Pending (user is deciding): pinning `UNDERSTAND_MODEL` / `SUMMARY_MODEL` + raising their token caps. Until then all three roles run `openrouter/free` with 256/400-token caps -- the silent-degradation risk documented on 2026-08-24 stands.

## Hide a catalog model without deleting it (complete 2026-08-24)

Discussed: the user selected the four paid entries in `backend/rag_ingestion/data/models.json` (`openai/gpt-5.6-sol-20260709`, `anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`, `anthropic/claude-haiku-4.5`) and asked to comment them out while testing OpenRouter's free tier.

That is not possible as asked. `load_catalog` parses the file with `json.loads`, which rejects `//` and `/* */`, and the two obvious workarounds are closed too: `ModelSpec` and `ModelCatalog` both set `extra="forbid"`, so neither a `_comment` key on an entry nor a second top-level `disabled` array survives validation.

### Decisions

**A field, not a deletion.** Deleting the four entries would have worked and git would have them back, but the user is mid-way through testing free models and will toggle these repeatedly, and each entry carries hand-tuned numbers (`keep_recent_max_messages` 150/120/60) that would have to be re-derived. `enabled: bool = True` on `ModelSpec` costs one line and keeps the numbers in the file.

**Filter in the route, not in `load_catalog`.** This was the one real design question. Filtering the catalog itself looks cleaner and is wrong twice over:

- `ApiContext.context_used_percent` and the streaming route both call `resolve(thread.last_model_id)`. A thread already running on a hidden model would fall back to the default's window, so its context gauge, its `keep_recent_max_messages` and its summary trigger would all silently change the moment the model was hidden. `resolve`'s own docstring reserves that fallback for models genuinely removed from the catalog, which is a different event.
- `resolve` ends in `next(spec for spec in self.models if spec.id == self.default)` with no fallback value. Filtering a list that happens to contain the default turns every unknown-id lookup into an uncaught `StopIteration`.

Filtering in `GET /models` gives the picker the hiding behaviour and leaves `resolve` intact. The frontend needs no change: `api.ts` reads `{default, models:[{id,label}]}` and does not count entries.

**Default is `True`.** Every existing entry omits the key, so the additive default has to be the visible one or the picker would come back empty.

### Implemented

- `rag_ingestion/schemas/model_catalog.py`: `enabled: bool = True` on `ModelSpec`.
- `rag_ingestion/api/routers/system.py`: `if spec.enabled` in the `/models` comprehension.
- `rag_ingestion/data/models.json`: `"enabled": false` on the four entries above. The other five, including the default `openrouter/free` and the three free models added this session, stay visible.
- `tests/test_model_catalog.py`: three tests -- an omitted key defaults to visible, the bundled default is offered, every disabled spec still resolves to itself.
- `tests/test_thread_api.py`: `_mixed_visibility_catalog` plus a test that `/models` omits the disabled entry.

### Status

Complete, partially verified. `models.json` was validated against the real `ModelCatalog` (9 entries, 5 offered, 4 hidden; default still offered; every hidden spec resolves to itself) by importing the schema module with the package `__init__` stubbed out.

The pytest suite could not run, for two reasons that predate this change: the `.venv` is out of sync with `pyproject.toml` (`langchain_core` and others missing -- `nlpo3` was installed to get past the first import error), and Postgres is down, which makes `conftest.pytest_configure` abort collection for the whole session. `uv sync` is not the fix: it would remove `pytest`, `pytest-cov` and `torch`. Run `uv sync --all-extras` and `docker compose up -d db`, then the four new tests.

## Move Postgres to Supabase and Qdrant to Qdrant Cloud (complete 2026-08-18)

Discussed: the user asked whether the cloud versions were in use yet. They were not. Everything ran on local containers: `postgres:16`, a `qdrant/qdrant` container, Redis, and `LocalMediaStore` on a volume. The only cloud services in play were the provider APIs (Cohere, Voyage, Jina, Datalab, OpenRouter) and Firebase Auth.

The user then added credentials to `.env` and asked to wire them up, saying data migration was optional because there are no users yet. R2 was out of scope for this round: no keys exist for it, so object storage is still local disk.

What the credentials actually were, after testing each one rather than assuming:

- Qdrant Cloud key and endpoint worked on the first try, on both `:6333` and `:443`. Cluster was empty.
- The Supabase keys in `.env` (`sb_publishable_*`, `sb_secret_*`) are REST keys. This backend talks to Postgres over psycopg2, so they are unusable for it; what it needed was a connection string, which was not in `.env`.
- `PASSWORD=eslothai-chat` turned out to be the real database password. `USERNAME=eslothai-chat` is not a database user, and is unused.
- The project's region could not be inferred. Its direct host resolves to `2406:da14:...`, which is AWS Tokyo, but probing 32 pooler hosts found the tenant only on `aws-0-ap-northeast-1`.

### Decisions

**One name per setting, no aliases.** `.env` used `CLUSTER_ENDPOINT`; the code has always read `QDRANT_URL`. Rather than teach `config.py` two names for one value, `.env` now uses `QDRANT_URL`. A `DATABASE_URL` had to be added regardless, because it cannot be derived from `SUPABASE_URL`: the pooler host is a different host entirely.

**Compose defaults stay local.** `DATABASE_URL: ${DATABASE_URL:-postgresql://...@db:5432/rag}` and the same shape for `QDRANT_URL`. Emptying the two variables in `.env` reverts to local containers. The local `db` service still starts while Supabase is in use, which is dead weight but keeps the switch back to a one-line edit.

**No helper for building the Qdrant client.** Two construction sites, one extra keyword argument each. A factory would have been more code than the thing it wrapped.

### Implemented

- `config.py` — `RuntimeConfig.qdrant_api_key`, read from `QDRANT_API_KEY`, `None` when blank or unset so a self-hosted cluster is unaffected.
- `api/app.py` — the key is passed to the `QdrantClient` the app opens.
- `scripts/benchmark/mtrag_run.py` — same, the second construction site. Without it a benchmark against the cloud cluster would have failed on auth.
- `indexing/qdrant_index.py` — `ensure_collection` creates seven payload indexes instead of six, adding `citable` as a bool. See below.
- `compose.yaml` — `DATABASE_URL` and `QDRANT_URL` overridable with local defaults, `QDRANT_API_KEY` passed through.
- `.env` / `.env.example` — `QDRANT_URL` replaces `CLUSTER_ENDPOINT`, `DATABASE_URL` added with a comment explaining the pooler host and port choice.
- `document/database/README.md` — new. Where each kind of data lives, the three ways a Supabase connection string goes wrong, collection naming, the strict-mode index rule, and how to switch back to local.
- Tests: 3 in `test_config.py` (endpoint and key read, blank key absent, `DATABASE_URL` read), 1 in `test_app.py` (the key reaches the client the app opens), 1 in `test_qdrant_index.py` (every field a filter names is indexed, `citable` as bool).

### The bug the move exposed

The first real query against Qdrant Cloud returned a 500. Underneath: `Bad request: Index required but not found for "citable"`.

Managed clusters run `strict_mode_config.unindexed_filtering_retrieve = false`. Self-hosted Qdrant answers the same filter with a scan, so this had been latent since `citable` was first filtered on. `ensure_collection` indexed six keyword fields and `citable` is a bool, so it was never covered. The fix is the index; the invariant worth keeping is that the list of indexes and the list of fields any filter names have to stay equal, which is what the new test asserts.

The search path deliberately does not call `ensure_collection`, so collections created by the old code do not pick the index up on their own. The two smoke-test collections were dropped and recreated rather than patched, which was free here because the cluster held nothing else.

### Verified

- `alembic upgrade head` against Supabase: 15 migrations, head `d5b1c8e3a9f7`, 15 tables.
- Container boots against both: startup log shows the cluster URL and a 200 from it, `/health` 200.
- Full path on cloud: create project, upload a PDF, 3 blocks, 3 chunks, 3 indexed, then a query returning the chunk with page bboxes and `citable: true`.
- One failure along the way was not ours: Cohere returned 429, so the smoke test ran on Jina.
- Full suite: 832 passed.
- Cleaned up: 3 test projects deleted, both stores verified back to zero rows and zero points.

### Still open

- R2 is wired as of 2026-08-18. See the R2 section at the top of this file.
- Redis is still a local container.
- `USERNAME` and `PASSWORD` remain in `.env` and nothing reads them. The password is now also inside `DATABASE_URL`, so the two can drift.
- `document/techstack.md` still describes a stage-1 stack (R2, Gemini embeddings) that does not match what is built.

## PPTX parsing: order, notes, structure, images (complete 2026-08-18)

Discussed: after the spreadsheet work the user reported pptx dropping images too, and asked for a full audit. Nine findings, proved by building a deck shaped like a real one and running it through the parser rather than by reading the code:

1. pictures dropped entirely
2. speaker notes attached to the wrong slide (part name guessed from slide position)
3. slides ordered by part name instead of `sldIdLst`
4. an image-only deck indexed as "completed" with zero chunks
5. a 1200-word slide became one chunk against a 512 limit
6. `section_path` empty, so a whole deck was one section
7. the whole slide rendered as one markdown heading, with notes mixed into the body
8. the slide-number placeholder swallowed into the notes text
9. tables on a slide flattened into a run of lines

User decisions: fix 1, 2, 3, 7, 9; apply 4 to every format, not just pptx; 8 low priority but do it; each slide becomes a heading; and for 5, keep one leaf per slide if markdown parsing was not feasible.

5 turned out not to need a decision. It was a symptom of 7: `SLIDE` is in the chunker's `_SPECIAL_TYPES`, which skips `_split_oversized`, and `PARAGRAPH` is the only type that gets split. Splitting the slide into title + paragraphs fixed the sizing for free — measured 512 / 512 / 176 on the 1200-word slide. `chunker.py` untouched.

Notes needed no renderer change either: `SPEECH_TURN` is already a special type (its own chunk) and already renders as a blockquote, which is what speaker notes are.

### Implemented

- `ingestion/parsers/office.py` — `parse_pptx` rewritten. `_slide_parts` reads `sldIdLst`, `_notes_text` resolves notes through the slide's relationships, `_shape_paragraphs` reads per `a:p` and drops `a:fld` text, `_table_rows` builds a real markdown table using the `grid` flag added for spreadsheets, `_store_slide_picture` reuses the media-store and magic-byte helpers. Hidden slides are kept and marked `hidden` in metadata rather than dropped — no data loss either way, and dropping them is not ours to decide.
- `ingestion/pipeline.py` — passes `media_store` to the pptx branch, and raises `No text could be extracted from this document` when no block has any text, for every format.
- Tests: 12 pptx tests added, 2 pipeline tests for the guard, and the old pptx test rewritten (it asserted the single-block shape and the notes-by-file-name mapping that were the bugs).

Verified on a deck whose presentation order is 3-1-2 with notes on slide 2 stored as `notesSlide1.xml`: order correct, notes on the right slide, `7` placeholder gone, figure stored, three sections, no leaf over 512 words, notes out of the body, every cited block anchored.

Status: complete. 825 backend tests green.

**Left open on purpose:** `_split_oversized` still runs for `PARAGRAPH` only, so a very long `TABLE_ROW`, `LIST` or `CODE` block in any format can still exceed the leaf limit. Separate from pptx, not fixed here.

## XLSX parsing: values, columns, sheets, embedded images (complete 2026-08-18)

Discussed: a real workbook (`.temp/Tiền ăn.xlsx`, a shared-meals ledger) rendered badly in the document panel. Investigation showed it was not a cosmetic problem — the indexed text was wrong.

Four defects found by reading the parser against the real file:
1. Row text came from the formula workbook, so the index held `=SUM(C16:H16)` instead of `38`. The cached value was already being read and stored in cell metadata that neither the renderer nor the chunker looks at.
2. Empty cells were filtered out before joining, so column positions shifted and a value ended up attributed to the wrong person.
3. Both sheets numbered rows from one, into a single flat stream.
4. 38 of 73 rows were fill-down formula tails, i.e. two thirds of the markdown was noise.

Then: the sheet's four QR images were being dropped completely.

Decided with the user: extract and render images, **no OCR for now**. Images display; their contents stay unsearchable. OCR through the existing `ScanParser` is the obvious follow-up but costs a provider call per image.

Approach was prototyped standalone in `.temp/xlsx_parser.py` and diffed against the original output before touching `backend/`.

### Implemented

- `ingestion/parsers/office.py` — `parse_xlsx` rewritten. Values over formulas, columns preserved, one heading + one markdown table per sheet, junk rows dropped, dates/numbers formatted, merged ranges written once. New `_sheet_pictures` reads drawing parts out of the zip (not `Worksheet._images`, which needs Pillow and fails silently without it) and sniffs the real image format from the magic bytes. Takes an optional `media_store`.
- `ingestion/markdown.py` — a `grid` metadata flag makes a run of blocks render as consecutive lines instead of separate paragraphs, and keeps the grid header alive even though it owns its rows. Everything without the flag renders exactly as before.
- `ingestion/pipeline.py` — passes `media_store` into the XLSX branch; it was already being passed to the pdf and image branches only.
- Tests: 11 added across `tests/test_office_parsers.py` and `tests/test_markdown.py`, plus the existing xlsx test updated (it asserted the formula-as-text behaviour that was the bug).

The per-workbook formula fallback was a defect the unit tests missed and only the real file exposed: deciding per cell put `=IF(...)` back into every cell that had correctly evaluated to blank.

Result on the real workbook: 149 markdown lines to 45, 72 chunks to 34, zero formulas in the output, four images stored, every cited block still anchored.

Status: complete for backend. 810 backend tests green.

### Document panel: rendering those images (complete 2026-08-18)

`urlTransform` turned out to be the wrong tool. The media route takes `X-User-ID` as a required header, so no plain `<img src>` can reach it however the URL is written. `DocImage` in `DocPanel.tsx` fetches through `getDocumentMedia` (the authenticated client) and renders an object URL, revoking it on unmount; `components={{ img }}` is passed to all four `ReactMarkdown` instances, including the three the citation highlight splits the document into.

Covered by one API test rather than a component test — the frontend has no test runner, and adding one is a dependency decision, not something to slip into this change. The test uploads a workbook with a picture, reads the key out of the returned markdown and fetches it back through the media route, which is exactly the path the panel walks.

This fixed PDF page images at the same time; they had carried a `media_ref` for much longer and had never once displayed.

## Google Login (full-stack, complete 2026-08-15)

Discussed: real auth to replace the dev token — Firebase ID tokens, backend verification, keep the anonymous-trial flow (3 messages) and its "log in to keep your history" promise.

Split across two sessions: one built the Firebase frontend (popup, `lib/firebase.ts`, Bearer headers in `api.ts`) and the backend verifier (`api/auth.py`, middleware, `AuthConfig` with X-User-ID fallback for anonymous visitors). This session completed it:
- `POST /auth/claim` moves an anonymous UUID's projects/threads/memories/settings/usage **and Qdrant tenant payloads** to the verified uid; Bearer-only, never the fallback.
- Service account mounted into the api container (`compose.yaml`); `auth=on` confirmed at startup.
- Login order in `Root.tsx`: save token → claim old UUID → switch userId; claim failure never blocks login.
- 616 backend tests green; live checks: fallback 200, fake Bearer 401, unauthenticated claim 401.

Status: complete, hardened 2026-08-15 (anon_ namespace, users registry, claim guards, token auto-refresh, Firebase signOut on logout).

Known remaining login items, by priority (1-3 done 2026-08-15: server-side ANON_MESSAGE_LIMIT with 403 -> login modal, fetchWithAuth 401-retry, pendingClaim retry):
4. Mobile: `signInWithPopup` is blocked by some mobile browsers; add `signInWithRedirect` fallback.
5. `verify_id_token(check_revoked=True)` (or short cache) so disabled/deleted Firebase users lose access before token expiry.
6. TTL cleanup job for never-claimed anonymous data (Postgres + Qdrant).
7. Apple/Facebook/Email/Phone providers (buttons render disabled).
8. Production deploy checklist: Firebase authorized domains, HTTPS, CORS.

## Firebase Google Login + Backend Verify

Status: complete (code), pending full backend runtime verification after local DB auth fix (2026-08-14).

### Discussed and decided

User provided both pieces needed to implement real auth: Firebase web config (previously at root `config.ts`) and backend service account JSON (`eslothai-chat-firebase-adminsdk-fbsvc-c52b09b3fd.json`).

Decision: keep API surface stable and avoid touching every route signature by introducing one auth middleware that verifies `Authorization` bearer token and publishes verified `uid` through request-local context; existing route code still calls `user_id(...)`, which now prefers the verified value.

### Implemented

Frontend:
- `config.ts` moved/renamed to `frontend/src/lib/firebase.ts`.
- Firebase client initialized there (`firebaseAuth`, `googleProvider`).
- `Root.tsx` now performs real Google sign-in (`signInWithPopup`) and stores session (`uid` + ID token).
- `storage.ts` now manages `loadAuthToken/saveAuthToken/saveSession`.
- `api.ts` now injects `Authorization: Bearer <token>` (if present) while preserving `X-User-ID`.
- `LoginPage.tsx` and modal now support async login state (`busy`, `error`), with non-Google options disabled.
- Added `frontend/.env.example` and typed Vite env keys in `frontend/src/vite-env.d.ts`.

Backend:
- Added dependency `firebase-admin` in `pyproject.toml`.
- New file `backend/rag_ingestion/api/auth.py`:
  - Firebase token verifier,
  - request-local user context via `contextvars`.
- `create_query_app` (`api/routes.py`) now has auth middleware:
  - verifies bearer token when Firebase auth is configured,
  - sets verified `uid` into request context,
  - supports optional legacy fallback when token is missing.
- `api/deps.py:user_id()` now returns verified `uid` from context first.
- `config.py` now includes `AuthConfig` (`FIREBASE_SERVICE_ACCOUNT_PATH`, `FIREBASE_PROJECT_ID`, `AUTH_ALLOW_X_USER_ID_FALLBACK`) and auto-detects `*firebase-adminsdk-*.json` from project root when path env var is unset.
- `.gitignore` now excludes Firebase admin key files (`*firebase-adminsdk-*.json`).

### Verify

- Frontend: `npm run build` passed.
- Backend: attempted `uv run pytest backend/tests/test_app.py -q` but local DB precondition failed (`role "postgres" does not exist` at `postgresql://postgres:password@localhost:5432/rag`), so full backend integration verification is blocked by environment.

### Files changed

- moved: `config.ts` -> `frontend/src/lib/firebase.ts`
- created: `backend/rag_ingestion/api/auth.py`, `frontend/.env.example`
- updated:
  - `frontend/src/lib/storage.ts`
  - `frontend/src/lib/api.ts`
  - `frontend/src/Root.tsx`
  - `frontend/src/components/LoginPage.tsx`
  - `frontend/src/styles/global.css`
  - `frontend/src/vite-env.d.ts`
  - `backend/rag_ingestion/config.py`
  - `backend/rag_ingestion/api/routes.py`
  - `backend/rag_ingestion/api/deps.py`
  - `backend/rag_ingestion/api/app.py`
  - `.gitignore`
  - `pyproject.toml`
  - `CHANGELOG.md`

## Tách api/routes.py theo domain

Status: complete (2026-08-13).

User review codebase thấy `api/routes.py` 2.157 dòng (27 endpoints + models + helpers trong 1 hàm factory closure) khó đọc cho dev mới; các module khác đã tách tốt. Quyết định: tách theo domain, giữ nguyên API và hành vi. Kết quả: `schemas.py` (models), `deps.py` (`ApiContext` + shared helpers thành methods), `routers/system|projects|threads|memory.py` mỗi file expose `register(app, ctx)`, `routes.py` còn factory 98 dòng có bản đồ file trong docstring. Không dùng `include_router` vì FastAPI 0.141 include lazy làm test introspect `app.routes` vỡ — decorator thẳng lên `app` giữ registration y hệt cũ. Suite 625 passed. Backup zip trong `archive/`.

## Token usage trong Settings → Usage

Status: complete (2026-08-13).

OpenRouter trả usage ở SSE chunk cuối, backend trước chỉ log. Giờ: `on_usage` callback xuyên `ChatCompleter`/`OpenRouterChatModel` → route thread-chat ghi accumulator `llm_usage` (1 row/user, migration `a9c5e2f7d3b8`) → `GET /usage` → tab Usage hiện Total/Input/Output tokens + LLM requests. TDD: test route-level với fake chat báo usage (624 passed). Mới ghi ở luồng thread-chat; understand/summarize/answer-cũ chưa nối (provider sẵn sàng). Chạy test local: cần `DATABASE_URL`/`TEST_DATABASE_URL` trỏ Postgres user hiện có (port 5432 trên máy dev bị brew postgres chiếm, docker bind bị che).

## Library

Status: complete (2026-08-13), chờ backend cho phần generated.

Item sidebar "Library" gom tài liệu user đã upload trên mọi project (tham khảo layout Grok: title + search + filter chips). Lọc source All/Uploaded/Generated — hiện mọi file là uploaded; khi backend có file do chatbot tạo thì sửa `sourceOf()` trong `LibraryPanel.tsx` đọc field source từ record. Search lọc theo tên client-side. Fetch khi vào view (song song mọi project). Verified puppeteer với backend thật (seed project + PNG upload, check rows/filter/search/empty states).

## Settings popup + Share

Status: complete (2026-08-12). Mở rộng 2026-08-13: nội dung Customize → Personalization chuyển hết vào popup — "About you" vào General, Memory vào tab mới Capabilities (icon briefcase). Dialog chuyển render lên App để cầm memories/settings/handlers; Sidebar chỉ giữ `onOpenSettings`. Customize còn mỗi Account (User ID + providers).

Bấm Settings trong profile menu mở modal (thay vì điều hướng): rail trái với General / Account / Usage / Share Esloth with others, tham khảo layout settings của Claude. General lưu Full name + nickname vào localStorage (`rag.fullName`, `rag.nickname`); Account hiện User ID + Log out; Usage hiện số chats/projects thật. Tab Share: màn "Spread the word" với nút Copy link copy chuỗi "I've been using EslothAI, an intelligent AI. Try it!\nhttps://eslothai.com" vào clipboard. Chuỗi UI để trong dict theo ngôn ngữ (structure sẵn cho vi, mới implement en theo yêu cầu). Verified full flow bằng puppeteer gồm cả đọc clipboard.

## Profile menu (Language + Get apps)

Status: complete (2026-08-12), mở rộng cùng ngày.

Đợt 1: bấm profile ở sidebar footer ra menu với Language (English/Tiếng Việt kèm quốc kỳ, lưu `localStorage rag.language` + checkmark — chưa dịch UI vì app chưa có i18n) và Get apps and extensions (bấm được, chưa có logic). Tenant/team bỏ qua.

Đợt 2 (theo screenshot Claude của user): Language mở bằng **hover** flyout bên phải (sidebar đổi `overflow: visible` để hết bị cắt, thêm hover-bridge vô hình qua khe 8px); menu đủ mục — header userId, Settings (→ Customize), Language, Get help, View all plans, Get apps, Learn more (flyout hover: About/Usage policy/Privacy policy/Keyboard shortcuts), Log out. Log out xoá token + reload về home. Mục chưa có backend chỉ đóng menu khi bấm. Verified full flow bằng puppeteer (hover mở flyout, chọn ngôn ngữ, điều hướng Settings, logout về home).

## Pi.ai theme cho frontend

Status: complete (2026-08-12). Đợt 2 cùng ngày: chuyển sang **dark mode** theo screenshot Pi thật của user (palette `.dark` trong spec), serif Lora → Fraunces (gần Recoleta/Grenette), composer serif bo 24px nền `#2C2A28`, nút gửi tròn xanh `#289A62`. hljs/mermaid về theme dark. Build + screenshot verify khớp ảnh tham khảo.

### Discussed and decided

User muốn dùng đúng theme của pi.ai/talk. Pi.ai chặn Cloudflare nên không xem live được; lấy CSS production từ Wayback Machine (snapshot 2026-08-02) và trích toàn bộ design tokens, font, typography scale, component patterns vào `document/pi-ai-theme-spec.md`. Phát hiện chính: Pi live dùng GT Alpina (serif chat) + ABC Oracle (sans UI) — cả hai trả phí — nên quyết định thay bằng Lora + DM Sans (free, DM Sans là chính font bộ brand mới của Pi).

### Implemented

Retheme qua lớp CSS variables (theme cũ dark kiểu Claude đã dùng vars nhất quán) + vá mọi màu hardcode:

```
frontend/index.html                 color-scheme light; Google Fonts DM Sans + Lora
frontend/src/styles/global.css      token block mới (palette Pi light, shadow nâu ấm,
                                    --accent-alt/--on-accent/--bg-bubble mới);
                                    ~25 chỗ hardcode dark được vá (scrollbar, modal
                                    backdrop, badge, switch, dropzone, buttons, v.v.);
                                    AI message serif 19px không bubble, user bubble
                                    #F3EADE bo 20px serif; sidebar thêm border phải
frontend/src/components/Message.tsx hljs github-dark → github (light)
frontend/src/components/Mermaid.tsx mermaid theme dark → neutral
```

### Verified

`npm run build` sạch. Headless Chrome chụp trang home và shell chat (dev bypass token): nền kem, heading serif xanh rêu, composer surface trắng ngà, nút xanh rêu — đúng palette Pi. Frontend không có test infra (không script test) nên verification là build + visual.

## Chat UI Table Stakes — Đợt 1 (frontend)

Status: complete.

### Discussed and decided

A competitive survey of 9 chat products (ChatGPT, Claude, Gemini, Perplexity, Copilot, Open WebUI, LobeChat, LibreChat, Vercel template) produced a 3-đợt roadmap. The user approved đợt 1 verbatim: stop generation, smart auto-scroll, copy message/code + syntax highlight, thread rename + confirm delete, regenerate + edit message.

One scope call made during implementation: regenerate and edit are **append-a-new-turn** semantics, not ChatGPT-style in-place branching, because the backend thread API (`api/routes.py`) has create/list/get/rename/delete/post-message only — no truncate or message-tree. True branching is a backend architecture change and was flagged instead of built.

### Implemented

```
lib/api.ts          isAbortError(); streamRequest passes AbortError through;
                    streamThreadMessage takes an optional AbortSignal
App.tsx             abortRef + stopStreaming(); partial text kept on abort;
                    scrollRef + stickToBottomRef + trackScroll() (120px threshold);
                    container scrollTop=scrollHeight replaces the bottom-sentinel
                    scrollIntoView (which stopped ~149px short — pre-existing bug);
                    regenerate(); renameThreadHandler() wired to existing renameThread()
components/Composer.tsx   onStop prop; Stop button replaces Send while busy
components/Message.tsx    CopyButton, CodeBlock (lang label + copy), UserMessage with
                          inline edit textarea; rehype-highlight added to the pipeline
components/Sidebar.tsx    Rename menu item + inline .recent-rename input;
                          two-step Confirm delete
components/icons.tsx      CopyIcon, RetryIcon, StopIcon
styles/global.css         msg-actions, code-block, edit-bubble, recent-rename;
                          pre-wrap moved off assistant bubbles onto error bubbles
package.json              + rehype-highlight, + highlight.js
```

Verified live in Chrome (dev stack on :5173/:8000): stop mid-stream keeps partial, all three scroll scenarios measured to the pixel, highlight + copy render on a real python block, rename persists, delete needs two clicks, edit sends the edited text. Build clean.

Đợt 2 (citation chips inline, full-text search, thumbs feedback) and đợt 3 (source-document panel, branching) not started; đợt 2's citation work has backend support already shipped (see the citations entry below).

### Feedback round (user review of đợt 1)

Decided and built:
- **Conversation outline** (`ThreadOutline.tsx`): dash column at the chat's right edge, click → popup listing user-message previews, click → smooth-scroll to that turn. User-specified UX, Grok-style minimap.
- **Delete chat modal** replaces the two-step menu confirm: "Delete chat / Are you sure you want to delete this chat? / Cancel · Delete".
- **Thumbs up/down**, display-only ("chưa cần API lưu") — component state, no persistence.

Answered, not built:
- Stop/retry history semantics documented (user msg persists pre-stream, assistant only on completion; abort = user msg without answer on reload).
- highlight.js theme scope confirmed (code blocks only).

### Feedback round 2

Built:
- **Answer versions**: retry folds into one turn with a `‹ 1/2 ›` pager (view-level grouping of consecutive identical user messages; storage untouched; survives reload).
- **Citation chips** (đợt 2.1 frontend): `[n]` markers → numbered chips, hover popover (breadcrumb + snippet), click opens/highlights the source entry. Live-hits only; reloaded messages wait for the đợt-3 panel.
- Outline rail resized to the requested 36×70.

Answered: đợt-3.4 markdown-edit design (see conversation 2026-08-11) — block-level diff via anchors, re-embed only chunks touching edited blocks, anchors sidecar recomputed in one pass, no re-chunking of unaffected chunks; chunk_id/block_id are content hashes so edited chunks get new ids (old citations to them degrade unless an alias map is kept).

### Feedback round 3 (2026-08-12)

Built:
- **Retry as atomic attempt**: previous answer hidden while regenerating; Stop rolls the attempt back (drop re-sent question + partial) so the turn reads as if Retry was never pressed. Normal-send stop still keeps partials.
- **Header breadcrumb** `{project}/{chat}`: project segment navigates to the project, chat segment renames inline, trailing chevron ("More option for {title}") opens the same Rename/Delete menu as the sidebar row. `ConfirmDialog.tsx` extracted and shared with Sidebar.

Answered:
- **Literal `[n]` collision**: current guards (grounded-only, 1..N, code/pre skipped) narrow but don't eliminate false positives. Recommended fix: switch the citation marker to a collision-proof token (`[cite:n]`) in `prompts/answering.py` + frontend regex — backend prompt change, needs a live regression check on citation compliance before shipping.
- **Đợt-3 panel readiness**: read-only Preview/Markdown panel is fully unblocked (markdown + anchors routes exist). The save/edit add-on still needs backend design (block-diff endpoint, selective re-embed, id alias). Right-sidebar button accepted into the panel spec.

### Feedback round 4 (2026-08-12)

Built:
- `[cite:n]` marker (user picked it): backend prompt + frontend regex; fresh threads verified live. Old threads may still imitate `[1]` from their own history — stays literal, by design.
- Static chips for reloaded messages (citations without hits).
- **DocPanel** (đợt 3 read-only): header Documents button → right panel, file dropdown, Preview/Markdown tabs. Verified on a real indexed DOCX.
- Header rename input sized to the pill it replaces; header chevron + sidebar ⋮ icons centered (button default padding was the culprit).
- **Infra bug fixed**: MEDIA_ROOT moved inside the persistent volume (`data/media`) — rendered markdown used to vanish on every api rebuild. Pre-volume documents need re-upload.

Next for đợt 3: citation click → open DocPanel at the cited block (anchors already returned by the API), then the edit/save add-on (needs backend design: block diff, selective re-embed, id alias map).

### Đợt 3 progress (2026-08-13)

- **Click-to-jump shipped**: chip → DocFocus{doc_id, block_ids, nonce} → panel opens on the document, Preview tab, cited block range highlighted (markdown split at block-aligned anchor offsets) and scrolled to center. Old per-message sources list replaced by a "n sources" count line.
- DocPanel also gained: resize handle (320–min(900, 70vw), persisted), Files column listing all project files with status, panel scoped to project chats under the fixed header, right-panel glyph on the files button.
- Remaining đợt-3 item: the markdown edit/save add-on (backend design pending).

Specs recorded for đợt 2/3 (user's refinements):
- **Citations inline in the answer**: hover = source popover, click = jump to position in file. `prompts/answering.py` already instructs bracketed `[n]` markers in grounded mode, and `citations` rows carry `{chunk_id, doc_id, block_ids}` — frontend chip rendering can map `[n]` → `hits[n-1]`. Click-to-jump lands with the đợt-3 panel.
- **Document panel**: two tabs, Preview (rendered markdown) and Markdown (raw). Add-on accepted for later: user edits raw markdown → save → detect edited region via anchors → re-chunk/update affected chunks in Qdrant (backend design needed: anchor invalidation, re-embedding).
- **Fork**: approved as normal-history fork (copy messages up to fork point into a new thread) — no in-place tree.
- Full-text search: pending (user's word).

## Markdown Rendering and Clickable Citations (backend)

Status: backend complete. Frontend not started.

### Discussed and decided

The user asked whether a citation could route to its position in the source file. It could not: the chat showed a bracketed number and nothing more. Investigating what would make it possible turned up four constraints that together ruled out the obvious approaches.

- `Block[]` is never persisted. Parse produces blocks, chunking consumes them, only chunks reach Qdrant.
- The upload is deleted. `_process_one_upload` works inside a `TemporaryDirectory`.
- `Chunk` carries no document order, only `source_block_ids` (content hashes) and `source_span` (an offset inside one block). A document cannot be reassembled from Qdrant.
- Only scanned pages get a PNG. `render_pages(path, scanned_pages, ...)` deliberately skips born-digital pages, and DOCX/XLSX/PPTX carry `OfficePathLocator`/`CellRangeLocator` with no page concept at all.

So "serve the original file" and "render pages on demand" were both out. The user's own proposal survived: render one markdown version per document at ingest and anchor citations into it. They refined it mid-discussion from "images route to the image, PDFs route to markdown" to "everything routes to markdown, but an illustrative image (one that is not carrying text) stays an image", which is what the renderer does.

Two decisions taken with the user:

- **Documents already indexed are out of scope.** "bỏ qua tài liệu đã up rồi đi, đây đang là giai đoạn dev, không có user." They 404 and cannot be re-rendered, since their blocks are gone.
- **Store the citation's target rather than looking it up.** The user pushed back on my first answer here ("với mỗi citation thì có sẵn block id rồi mà nhỉ?") and then asked me to check rather than guess ("check kỹ lại và cho câu trả lời"). Checking changed two of my claims: enriching `citations` needs no migration (the column is JSON text), and Qdrant lookup does *not* detect a re-indexed document better than stored anchors (`chunk_id` and `block_id` are both content hashes, so both change together). With that symmetry established, the decision came down to round trips and resilience, and storing won.

### Implemented

```
ingestion/markdown.py         new: pure render(document, blocks) -> (markdown, anchors),
                               plus store() and markdown_keys(doc_id)
indexing/qdrant_index.py      fetch_chunks: scroll+filter -> retrieve by uuid5 point id,
                               tenant check moved into Python; shared _point_id() helper
api/routes.py                 render+store before indexing; GET .../{doc_id}/markdown and
                               GET .../media/{key}; owned_document() helper;
                               citations built from hit.doc_id + hit.source_block_ids
storage/threads.py            new Citation model; ThreadMessage.citations is now
                               list[Citation]; before-validator reads old bare-string rows
storage/pg_threads.py         dumps model_dump()s instead of raw strings
```

Design points worth keeping:

- **A sidecar offset index, not inline anchors.** A chunk spans several blocks, so the panel highlights a range. Offsets express that; `<a id=...>` does not. It also keeps the markdown clean enough to read on its own.
- **The renderer duplicates the chunker's dedup rules on purpose.** They must agree or a citation lands nowhere, silently. The comment in `markdown.py` says so, and the invariant test runs real `chunk_document` output against real `render` output rather than asserting the rules twice.
- **Markdown is written before indexing.** An orphan markdown file after a failed index is harmless; an indexed document with no markdown is a dead citation.
- **Headings render but are never anchored.** The chunker excludes them from chunks entirely, so an anchor there would be unreachable.

### Caught during verification, not requested

The first traversal test passed for the wrong reason. `httpx` collapses `..` out of a URL path before sending, so `f"{doc_id}/../../etc/passwd"` arrived as `/projects/{id}/etc/passwd` and 404'd on routing without ever reaching the guard. Confirmed by printing `httpx.Request(...).url.path`, then rewrote the test to send percent-encoded dot segments and assert the 404 body is `"Media not found"` — a string only the handler produces. Added a Windows-shaped `..%5c..%5c` probe and a cross-tenant case (a second user constructing a valid key and trying it against their own project), since guessable keys are the realistic attack: `doc_id` hashes content the uploader already has.

### Verified

`583 passed` (was 569), green on three consecutive full runs. Live: generated a real two-page PDF with pymupdf and ran it through the real `process_document` → `chunk_document` → `render`/`store` path outside any test harness — 4 blocks, 3 chunks, 4 anchors, zero cited blocks missing an anchor, and every `markdown[start:end]` sliced back the exact block text.

### Open

- **Frontend is not built.** The right-hand document panel, click-to-scroll, and highlighting are all still to do. The backend contract they need is `GET /projects/{id}/documents/{doc_id}/markdown` plus `citations[].block_ids`.
- **`AnswerResponse.citations` on `/projects/{id}/answer` is still `list[str]`.** Only the stored thread-message citations were enriched. That endpoint's response already carries `hits` with `doc_id` and `source_block_ids`, so a client can join, but the two `citations` fields now have different shapes.
- **Figure cropping.** A `FIGURE` block points at the whole-page image, not a crop of itself. Datalab already returns figure bboxes, so the data exists; the work does not.
- **Media is never cleaned up on project delete.** Same orphan class as the Qdrant vectors project delete already leaves behind.
- **`test_review_round2.py::test_a_disconnect_mid_upload_still_invalidates_the_cache` flaked once** on the first full run after this change (the version key was not bumped, meaning both uploads failed) and has not reproduced in three full runs since. It passes in isolation and nothing in this change touches it. Not root-caused. Suspicion, unproven: apps built without `TestClient` never fire FastAPI's shutdown event, so their upload executors leak threads across tests.
- **`rag-api` will not start**, unrelated to this work: `DuplicateTable: relation "threads_recency_seq" already exists` on migration. Left alone. This is why the live check ran the pipeline in-process instead of over HTTP; the HTTP surface is covered by 11 route tests against the real app on real Postgres.

## Review Round 2: Cache Lifecycle and Never-Raises Contracts

Status: complete.

### Discussed and decided

- A second automated review (`/code-review`) raised 10 findings against the previous round's diff. Each was verified against the code before anything changed: 6 confirmed as patches, 3 real but pre-existing (deferred: the 8-14 message summary gap, unbounded proposal growth, inert `HISTORY_MAX_MESSAGES`), 1 dismissed as a false positive (reference expansion "dead code" -- `chunk.refs` is fed by reconciliation relations, not only the deleted `link_references`).
- Decisions taken with the user, in their words where it mattered:
  - Embedding profile stays pinned per project ("Giữ nguyên như hiện tại"). Changing the embed model 503s old projects by design; that is the re-index signal. The alternative (provider-only pinning) was rejected because the collection name carries no model name, so an un-bumped profile version would silently mix two vector spaces in one collection.
  - Reranker model goes into the cache key only, NOT pinned per project: it stores nothing, so pinning would only create upgrade lock-in.
  - The version counter stays in Redis (user's argument, accepted over my Postgres proposal: it serves only the cache, and losing both together is harmless). The dangerous half-loss is eliminated by `volatile-lru` instead -- verified that the counter is the single no-TTL key in the whole codebase.
  - Reference truncation happens on our side at `reference_cap * 2`, derived from the 1-block-to-1-leaf invariant rather than guessed.
- Scope note: `load_memory`'s recall path was hardened alongside `write_understanding` (same contract, same outage, fails the turn even earlier). Discovered because the outage test could not reach `write_understanding` -- recall 500'd first.

### Verification

- `552 passed` (was 546). Postgres restarted via `docker compose up -d db` for the run.
- The disconnect test drives `StreamingResponse.body_iterator` directly and `aclose()`s it at the first yield; TestClient cannot express a disconnect (it always drains). RED confirmed by restoring the pre-fix `routes.py`.
- The reranker-key and truncation tests are plain RED->GREEN; the memory-outage test asserts the turn streams `role:assistant` with the store hard-down.

### Deferred (pre-existing, not caused by these rounds)

- Threads 8-14 messages long have turns in neither the replay window nor the summary (`keep_recent=6` vs `trigger=8`).
- `memory_proposal` grows without dedupe or cap when a scope stays full.
- `HISTORY_MAX_MESSAGES` no longer binds (window returns at most `keep_recent + 1`).
- Architecture item for a future decision: multi-profile registry so changing the embed model does not 503 old projects.

## Search Chats and Projects Modal

Status: complete. Planned formally (plan mode, user-approved) given the multi-file scope: a new component, a repurposed sidebar button, keyboard navigation state, and a new relative-time helper that didn't exist yet.

### Discussed and decided

- The sidebar's search button already existed (`Sidebar.tsx`) but was mislabeled — `aria-label="Search chats"` while its `onClick` just navigated to the Chats list view. It never searched anything. The user shared a screenshot of claude.ai's real search popup and asked for the equivalent here.
- Explored first rather than guessing: confirmed `Sidebar.tsx` already receives `threads`/`projects`/`onSelectThread`/`onSelectProject` as props from `App.tsx` (which wires them to its real `openThread`/`openProject` functions), so the modal needed zero new prop plumbing through `App.tsx` — it lives entirely inside `Sidebar.tsx`'s existing props. Kept the diff smaller by not touching `App.tsx` at all.
- Reused three existing patterns instead of inventing new ones: `NewProjectDialog.tsx`'s modal shell (`.modal-backdrop` + Escape-effect), `Panels.tsx`'s lowercase-substring filter technique, and the existing `ChatIcon`/`ProjectsIcon` icons already used this way elsewhere in `Sidebar.tsx`.
- No relative-time bucketing (`Today`/`Yesterday`/`Past week`/`Past month`) existed anywhere in the repo — `Panels.tsx`'s `formatDate` only produces absolute dates. Wrote a small new helper scoped to this component rather than generalizing it prematurely (only one caller needs it right now).

### Implemented

```
components/SearchDialog.tsx   new: merges threads+projects, filters, sorts,
                               keyboard nav (arrows/Enter/Escape), relative-time labels
components/Sidebar.tsx        search button now opens SearchDialog instead of
                               navigating to the Chats view
styles/global.css             .search-modal, .search-input-row, .search-results,
                               .search-result-row/-title/-hint/-time, .search-empty
```

### Caught during verification, not requested

The new search `<input>` had no `id`/`name`, tripping Chrome's own a11y console warning. Confirmed it was specifically mine (not a pre-existing issue) by watching the warning's count increase by exactly one when the modal opened, against a baseline of pre-existing unrelated warnings on the composer textarea and file upload inputs elsewhere. Fixed silently; left the pre-existing unrelated ones alone since they're out of scope for this change.

### Verified

`tsc -b` and `vite build` clean. Live in the browser against a real logged-in session with real threads and projects (not test fixtures): opened the modal, confirmed recency-sorted listing with correct relative-time labels, typed a query and confirmed substring filtering across both threads and projects, arrow keys moved the highlight with the "Enter" hint following it, Enter opened the highlighted result and closed the modal, clicking a result directly opened it and closed the modal, Escape closed without selecting. Console clean apart from the pre-existing unrelated warning.

### Open

- Result cap is 30; not surfaced to the user if there are more matches than that. Not asked for, and the existing sidebar lists have no such indicator either, so left consistent with that precedent.
- No fuzzy matching — exact substring only, same as `Panels.tsx`'s existing project search.

## Backend Logic Fixes from CODE_REVIEW.md

Status: complete. Documentation drifts (DOC-01..07) deliberately out of scope.

### Discussed and decided

- Reviewed `CODE_REVIEW.md` against the code first. All 14 logic findings reproduced; no false positives. Test count and warnings matched the report exactly (`458 passed`).
- Three corrections to the review were agreed before any code changed:
  - HI-02's line reference pointed at `recall()`, not `update_memory`. Its blast radius is also narrower than implied: the *automatic* promotion in `routes.write_understanding` already went through `add_memory`, so only the manual `PATCH /memories/{id}` path skipped the invariants.
  - HI-01 was understated. The report said the summary watermark "may" stop advancing; it stops permanently and by arithmetic, because `len(messages)` saturates at the read limit. Treated as Critical alongside CR-01 on the grounds that a loud deploy-time failure is less dangerous than silent corruption of every long conversation.
  - One finding was missing: reference expansion sorted its candidates by a score none of them could have, so `reference_cap` truncated in Qdrant point-id order.
- HI-05 turned out to be hardening rather than a live bug, discovered while writing the test: `RankedResult.index` is already `ge=0` and the HTTP adapter maps the resulting `ValidationError` to `ProviderResponseError`, so no shipped reranker can emit a negative index. The chain check was still tightened, since the `Reranker` Protocol has no such gate.
- ME-06 had two options in the review. Chose refusing `type=pin` at the API over implementing the retrieval filter: implementing it is a feature, not a fix, and the smaller change makes the API honest today. This is a breaking contract change and was called out as such.
- HI-04 was implemented with both an advisory lock and a partial unique index rather than one or the other. The lock serializes the store's own writers; the index is what holds the invariant if a future writer forgets the lock.

### What was implemented

| Finding | Fix | Where |
|---|---|---|
| CR-01 | Four `create_table` calls written into the empty revision, `memory_audit.sequence` left for the next revision | `migrations/versions/82a8b357280a_*.py` |
| HI-01 | `list_messages` returns the newest slice; `refresh_summary` counts and fetches by position; opener fetched back for `window` | `storage/pg_threads.py`, `api/routes.py` |
| HI-02 | Promotion routed through `_claim_slot` with `exclude` | `storage/pg_memory.py` |
| HI-03 | Proposal locked `FOR UPDATE`, memory written and proposal deleted in one transaction | `storage/pg_memory.py` |
| HI-04 | `pg_advisory_xact_lock` on owner/scope/target, plus `memory_live_key_idx` | `storage/pg_memory.py`, `db/tables.py`, new migration |
| HI-05 | `0 <= index < len(pool)` | `retrieval/chain.py` |
| HI-06 | `_check_vector` on both index and query paths | `indexing/qdrant_index.py` |
| ME-01 | Unrequested Datalab pages skipped; out-of-document pages rejected | `parsers/datalab.py`, `parsers/pdf.py` |
| ME-02 | Zero blocks is a parse failure, not confidence 1.0 | `parsers/pdf.py`, `parsers/image.py` |
| ME-03 | Payload shape guards, coercions mapped to `ProviderResponseError` | `parsers/datalab.py` |
| ME-04 | Geometry and time-span validators; corner order normalized in the adapter | `models.py`, `parsers/datalab.py` |
| ME-05 | Postgres sequence `threads_recency_seq`, seeded above the old max | `db/tables.py`, `storage/pg_threads.py`, new migration |
| ME-06 | `type=pin` refused with 422 | `api/routes.py` |
| LO-01 | Ownership tracked for engine and Qdrant client, closed on shutdown | `api/app.py` |
| new | Reference weights taken from ranked leaves | `retrieval/chain.py` |

Two changes were needed that no finding asked for. `migrations/env.py` accepts an injected connection so the chain can be driven into a throwaway schema, and its `fileConfig` call now passes `disable_existing_loggers=False`; the default silenced the application's loggers and broke nine unrelated log assertions the moment Alembic ran in-process. `ConversationConfig` gained `summary_max_fold_messages`, because fetching the unfolded span by position removes the accidental bound the old 200-message read gave it.

### Verification

- `545 passed` (was 458), coverage 96%.
- Every fix was watched fail first. The migration test reproduced the review's exact `UndefinedTable: relation "memory_audit" does not exist`. For the window fixes, `routes.py` and `pg_threads.py` were restored from backup to confirm three of the five new window tests fail on the old code, then restored.
- Concurrency was tested by asserting invariants and by holding the advisory lock from a second session, rather than by racing threads and hoping they overlap.

### Not done

- DOC-01 through DOC-07. These are documentation edits and were left for a separate pass.
- The PIN retrieval filter. Refused at the API instead; implementing it is a feature decision.

## Retrieval Outcome Cache in Redis

Status: complete, shipped disabled by default.

### Discussed and decided

- Came out of an audit of what else Redis could do. EngCoach was read for prior art and uses it three ways: a JWT denylist keyed by `jti` with TTL equal to the token's remaining life, a `get_current_user` cache with a 60s TTL plus explicit invalidation at three call sites, and `fastapi_cache` on two endpoints with a **user-scoped key builder**. Industry sources add semantic caching, rate limiting, and session state.
- **Semantic caching was rejected**, not deferred. Redis' own documentation says the core difficulty is threshold tuning and that too loose a threshold "serves wrong answers". For a product whose value is a citation to a specific clause, answering a question about Điều 5 from a cached answer for Điều 6 because the two are "similar enough" breaks exactly what is being sold. It suits FAQ helpdesks, not clause lookup.
- **Conversation state was rejected too.** Threads and messages already live in Postgres, which is the stronger choice: a conversation is durable data, not a cache. The industry advice to keep it in Redis is aimed at products that have no durable store for it.
- Two findings outranked all caching and were reported rather than built, because the user deferred rate limiting and asked for the cache: there is **no rate limiting anywhere** despite three paid provider APIs, and there is **no authentication** — identity is a raw `X-User-ID` header, so anyone can read anyone's projects, threads and memory.
- Shipping it **disabled by default** was the compromise between building what was asked and my standing objection that nobody has measured the repeat-query rate. Hit and miss log at INFO so the objection resolves with data.

### The two rules that make it safe

- **Every input that shapes the result is in the key** — owner, project, query, rewrite, embedding profile, whole `RetrievalConfig`, limit, language. A parametrised test changes each one in turn and asserts a miss.
- **A completed upload invalidates the project**, via a Redis version counter that is part of the key. A failed upload deliberately does not bump: nothing was indexed, so nothing became wrong, and bumping would discard every valid entry over one bad file. Invalidation is emitted before the `done` SSE event so a client querying the instant it sees `done` reads through the new version.

Counter-bump rather than `SCAN`-and-delete: scanning a large keyspace on the request that just finished an upload is slow, and orphans age out under the TTL and the `allkeys-lru` policy already configured on the container.

### Verified

478 tests pass.

The end-to-end proof uses a **counting reranker** rather than asserting on cache internals, because the reranker is the expensive call and therefore the honest witness: two identical queries reach it once with the cache on, twice with it off, and two different questions reach it twice.

### Two mistakes worth recording, both mine

- The first `FakeRedis` gave `incr` and `get` separate dicts. The version counter was written where the read could not see it, so the invalidation tests failed — correctly, but against a double that did not model Redis. After fixing the fake I **checked the real thing**: `INCR` and `GET` do share a keyspace, and `GET` returns `b'1'`, which is why the implementation decodes bytes. A test double that diverges from the system it stands in for turns a passing suite into a false negative.
- Adding `retrieval_cache=%s` to the startup log added the argument but not the placeholder, because the format string spans three source lines and my replacement matched only one of them. Nine tests failed with `TypeError: not all arguments converted`.

### Open

- **The hit rate is still unknown**, which is why the feature is off by default. Turn it on in a deployment where the same questions recur, read the hit/miss lines, and decide from there.
- **`embed_documents` is deliberately not cached.** Document vectors already live permanently in Qdrant; caching them in a 24h store would be caching something that already has a durable home. The real waste there is re-embedding on re-upload, and the fix is to skip embedding for `chunk_id`s Qdrant already holds, not another cache layer.
- The query-embedding cache stays as it is. Once the "embed once, pass the vector down" fix lands, its main source of hits (the same query embedded three times within one request) disappears and it will rarely hit — harmless, but not worth further investment.

## Login Page at /login

Status: complete (UI only, by design). Planned formally (plan mode, user-approved) given the architectural questions involved: routing approach, and how much of "login" to actually build with no auth backend in place.

### Discussed and decided

- Two blocking questions asked before any code: (1) should the Google/email buttons be real auth or UI-only — answered UI-only, since the app has zero auth infrastructure today (just an anonymous UUID in `storage.ts`) and real OAuth is a separate, much larger piece of work; (2) where does the page live — answered "a real URL, e.g. `{domain}/login`", not just an internal view swap.
- Fetched the live claude.ai/login page in a real browser (WebFetch hit a 403) to confirm the screenshot's structure against the actual site. The live page also has pricing tiers, an FAQ, and a large multi-column footer that aren't in the user's screenshot and weren't asked for — deliberately not built, to keep this a login page and not a full marketing site clone.
- No routing library. Exactly one alternate route needs to exist (`/login` vs. everything else), and this codebase's own pattern is to reach for a dependency only when the standard toolkit genuinely can't do the job (`mermaid`/`katex` were added only once actual markdown rendering needed them). A `window.location.pathname` + `popstate` check in a new `Root.tsx` covers this without one.
- "UI-only" extended to the nav honestly rather than selectively: Platform/Solutions/Pricing/Resources/Contact sales/Download desktop app all render `disabled`. A button that looks normal and does nothing on click is a worse experience than one that visibly isn't wired up — same reasoning the model picker in the composer already established earlier this session.
- Hero photo: Anthropic's own asset, not reusable. Confirmed with the user and replaced with a labeled placeholder box rather than substituting an unrelated stock photo or guessing at what they'd want there.
- Product naming for the "Meet EslothAI" dropdown confirmed with the user: **EngCoach** (external, `eslothai.com`) and **EslothAI Chat** (this app, matches the existing sidebar wordmark).
- Login state is real, just not real auth: `isLoggedIn()`/`setLoggedIn()` in `storage.ts`, the same localStorage shape as the existing `loadUserId`/`saveUserId`. Every "continue"/"try" action sets it and enters the app. This gives a working, testable, persisting flow instead of a dead-end mockup, while still being honest that no server-side session exists.

### Implemented

```
Root.tsx                    new: pathname + popstate tracking, login gate,
                             now the actual root rendered by main.tsx
components/LoginPage.tsx    new: nav, "Meet EslothAI" dropdown, hero,
                             login card, all matching the screenshot
lib/storage.ts              isLoggedIn(), setLoggedIn()
components/icons.tsx        GoogleIcon (standard 4-color mark)
main.tsx                    renders <Root /> instead of <App /> directly
styles/global.css           .login-* classes, reusing .field/.primary-btn/
                             .secondary-btn/.model-menu rather than new ones
```

### One thing caught before calling it done

The first pass gated correctly (rendered `<LoginPage>` when not logged in) but only in React state — the address bar still showed `/` while the login page was on screen. Visiting `/` while logged out needs to actually become `/login` in the URL, not just render different content under the old one, or a reload would silently show something different than what the URL says. Fixed with a `history.replaceState` in the same effect that decides to gate, caught by checking `window.location.pathname` directly in the browser rather than trusting that the visual match meant the URL was right too.

### Verified

`tsc -b` and `vite build` clean. Live in the browser: `/login` reachable directly and via the redirect, with the URL confirmed correct both ways (this is exactly the thing the first pass got wrong). Opened the "Meet EslothAI" dropdown and saw both products. Clicked "EslothAI Chat" and confirmed it entered the real chat app — with the existing user's projects and threads still intact, since login only gates the shell, not the anonymous user id underneath — and the URL became `/`. Reloaded and confirmed the session persisted. Navigated to `/login` again while still logged in and confirmed it still shows the login page rather than bouncing back into the app. Console clean throughout.

### Proportion fixes against the reference (seven follow-up rounds)

The user flagged the visual proportions as "hoàn toàn sai lệch" (completely off) against claude.ai/login and asked for a re-fetch to correct it. The live re-fetch hit a Cloudflare Turnstile "Verify you are human" challenge this time (the first fetch during planning had succeeded); did not attempt to click through it, since that's bot-detection evasion rather than normal browsing. Worked from the user's own screenshots instead, both times this came up.

- **Round 1** (hero image/heading): the placeholder was a short, roughly-landscape, centered box in an even `1fr 1fr` grid; the reference is a tall portrait rectangle, top-aligned, in an uneven split favoring the text column. Changed `.login-hero-image` to `aspect-ratio: 4/5` with a `max-width: 640px` cap (an early attempt without the cap made the image comically tall, since `aspect-ratio` scales off column width and the column was too wide), `.login-hero` to `align-items: start` and `1.15fr 1fr` columns, bumped the heading to 60px.
- **Round 2** (nav position, dropdown interaction, card shape): user pointed out the nav links were left-aligned near the logo instead of clustered right near the CTAs (`.login-nav-links` needed `justify-content: flex-end`), the "Meet EslothAI" dropdown needed hover-to-open instead of click-to-open (added `onMouseEnter`/`onMouseLeave`, kept the click/Escape/click-outside handling for keyboard and touch), and the login card's buttons/input needed to be fully pill-shaped (`border-radius: 999px` on the Google button, the email field, and a new `.login-email-btn`) with the "OR" divider as plain text and no flanking lines.
- Verifying round 2 in the browser surfaced a real, unrelated-to-the-ask a11y defect via Chrome's own console: the email input had no `id`/`name`/`autocomplete`. Fixed silently (id="login-email", name="email", autoComplete="email") since it's a genuine bug in code already being touched, not scope creep.
- Verified each round: `tsc -b` and `vite build` clean, live browser screenshots compared against the user's reference crops, the hover interaction confirmed with `mcp__chrome-devtools__hover` (not a click), console clean.
- **Round 3** (real hover bug, card layering, desktop button removed): the round 2 hover fix opened the dropdown correctly but round 2 didn't account for the 6px gap between the trigger and the absolutely-positioned menu (`.login-products-menu { top: calc(100% + 6px) }`) — that gap isn't part of the wrapper's hoverable box, so moving the mouse down through it fired `mouseleave` before the pointer reached the menu, closing it before it could be clicked. Fixed with a hover-intent delay: `onMouseLeave` schedules a 250ms close, `onMouseEnter` cancels it. Also removed "Download desktop app" entirely per direct request (superseding the "disabled, not missing" reasoning from the initial build — the user decided it shouldn't exist at all, not just be inert), and fixed the login card's layering: the Google button and email input were filling with the exact same `var(--bg-raised)` as the card wrapper around them, so both blended into it; the reference has no card border at all, just individually-filled pill controls. Removed the card's border/background, removed the border from the Google button and email field, gave both an explicit `var(--bg-raised)` fill of their own.
- Verified round 3 the same way, plus confirmed the hover fix specifically by hovering the trigger and then the menu item in one continuous sequence (crossing the same gap the bug lived in) and clicking through — confirmed the real login flow worked end to end, then cleared `rag.loggedIn` from `localStorage` afterward so verification didn't leave a different state behind.
- **Round 4** (ground truth from the real markup): the user pasted claude.ai/login's actual rendered HTML/classes instead of another screenshot, which corrected a mistake in round 3's reasoning — the reference card is not borderless; it has `border-0.5 border-border-300 rounded-[2rem] bg-surface-1` and a soft shadow. Round 3 had removed the card's border/background entirely on the theory that the reference used no card at all. The real structure is three layers (page → card, subtle → controls, distinctly lighter), not two. Restored the card's border and a page-matching background plus a shadow, kept the round-3 button/field fill fix (that part was right), and pulled exact values straight from the classes: `rounded-[2rem]` → 32px, `p-7` → 28px, `max-w-md` → 448px, the form's `gap-4` between input and submit → 16px.

- **Round 5** (left-column centering): the user pointed out the heading, subheading, and login card were left-aligned within `.login-hero-copy` instead of centered. Confirmed by the pasted claude.ai markup: both headings carry `text-center` and the card itself has `text-center mx-auto`. Added `text-align: center` to `.login-hero-copy` and `margin: 0 auto` to `.login-card`. That cascade also would have centered the email input's own text against the reference's left-padded look, so added `text-align: left` on `.login-email-form .field` to counteract it.
- **Round 6** (email field height, temporary): per a direct request, bumped `.login-email-form .field`'s height from the shared `.field` class's 40px to 46px. Superseded by round 7 below once hard reference data arrived.
- **Round 7** (FAQ + Plans sections, and the height/fill correction that came with them): the user shared claude.ai/login's "Explore plans" and FAQ sections and asked to add both below the hero, plus Chrome DevTools element-inspector screenshots of the real email input (390.67×40px, `padding: 0px 16px`, background `color(srgb 1 1 1 / 0.05)` — a translucent white overlay, not a solid hex). This directly contradicted round 6's 46px bump, so asked one clarifying question rather than silently overriding an explicit prior request; the user chose to match the DevTools evidence. Reverted the field to 40px, added `padding: 0 16px`, and changed both the field's and `.login-google-btn`'s background from solid `var(--bg-raised)` to `var(--bg-hover)` (already `rgba(255,255,255,0.055)` in this app — an almost exact match to the measured 0.05 alpha). A second clarifying question (English vs Vietnamese copy for the new sections) confirmed English, matching the rest of `/login`.
  - Before writing any Plans copy, an Explore agent grepped the whole repo for tier/quota/billing logic and confirmed **none exists anywhere** — the three tiers (Starter 29.000₫, Pro 49.000₫, Max 99.000₫ per month) are presentation-only, same as every other CTA on this page. Feature bullets are limited to real, confirmed capabilities (project organization, PDF/DOCX/XLSX/PPTX ingestion — confirmed against `NewProjectDialog.tsx`'s `ACCEPT` constant and the backend's `_ROUTES` map, citations via `Message.tsx`'s `.sources` block, per-project embedding provider choice, cross-session conversation memory) plus soft non-numeric bullets on higher tiers, deliberately avoiding fabricated hard limits.
  - FAQ reuses the exact `<details>`/`<summary>` accordion already in `Message.tsx`'s `.sources` block (native, no custom toggle state, marker hidden via `::-webkit-details-marker`) rather than inventing a new disclosure pattern.
  - This supersedes the very first build's decision (see "Discussed and decided" above) not to build pricing/FAQ sections — that call was right when nothing had been asked for; it changed when the user explicitly asked for both.
- Verified every round: `tsc -b` and `vite build` clean, live browser screenshots compared against the user's references, console clean. Round 7 additionally verified the FAQ accordion expands/collapses and is keyboard-operable, all three plan cards render correct pricing, the Team tab is visibly inert, and a plan CTA correctly enters the app end to end — then cleared `rag.loggedIn` from `localStorage` afterward.

### Round 8: splitting Home from Login, Google-only, and a flagged real-auth gap

Planned formally (plan mode again) — this was a genuine restructure, not another proportion tweak: everything (hero, Plans, FAQ, and the login card) had lived on one `/login` page; the user wanted the marketing content moved to `/` (Home) and `/login` rebuilt from scratch as a minimal split-screen screen, referenced from `https://eslothai.com/login`.

- **Live reference check**: fetched `eslothai.com/login` in a real browser. First screenshot showed the *dashboard*, not a login screen — the browser's existing session cookie was still valid, and the real site redirects an already-authenticated visitor straight past login. Cleared `localStorage`/cookies for that origin and re-fetched to see the actual logged-out screen: one continuous background (no seam between the two halves), brand + heading + subheading + a small handwritten-style quote on the left, a floating card with only a "Continue with Google" button, a plain divider (no "OR" — there's only one method), and legal text on the right. No nav bar at all. This directly informed the new `LoginPage.tsx`.
- **Google-only, confirmed against EngCoach's actual code, not assumed**: the user asked for login "như EngCoach" (like EngCoach). An Explore agent read EngCoach's real code and confirmed its Google button is a complete pipeline — Firebase Auth popup → backend verifies the ID token via Firebase Admin SDK → creates a real user row → issues the app's own JWT → JWT middleware on every request — not a mockup. EslothAI Chat has none of that today (no user table, no session/JWT anywhere in its backend). Presented this finding plainly and asked whether to build the real thing or keep this UI-only; the user chose **UI-only, but flagged for later** — see Open below.
- **The test-token ask**: the user wanted a way to jump into any screen while testing without clicking through the UI each time. EngCoach itself has no dev-bypass mechanism to borrow (confirmed by the same agent). Rather than inventing a new mechanism, `storage.ts`'s existing boolean `rag.loggedIn` flag became a token-shaped one: `isLoggedIn()` now checks for any non-empty string under `rag.authToken`, and a new exported `TEST_AUTH_TOKEN` constant is the fixed value `setLoggedIn(true)` writes there. This means a real JWT can replace `TEST_AUTH_TOKEN` later with no call-site changes anywhere. `Root.tsx` also logs a one-line `console.info` dev hint with the exact `localStorage.setItem(...)` command whenever the visitor isn't logged in, so the token doesn't have to be hunted for in source.
- **Routing simplified to four cases**, not the six discussed earlier in conversation, because this app has no deep-linkable routes besides `/` and `/login` — "any other path while logged out" can just show Home rather than needing a dedicated redirect: `loggedIn && '/login'` → redirect to `/` (matches the live eslothai.com behavior confirmed above); `loggedIn && anything else` → the app; `!loggedIn && '/login'` → `LoginPage`; `!loggedIn && anything else` → `HomePage`.
- **`HomePage.tsx` is the old `LoginPage.tsx`'s nav/hero/Plans/FAQ content**, moved with its CSS classes renamed `.login-*` → `.home-*` (a correctness rename since "login" now names something else entirely, not a restyle — nothing about how it looks changed). Its inline login card (Google button, divider, email form, legal text) is gone, replaced with a "Get Started" button and a "Log in" hint, both navigating to `/login` instead of logging in directly — every CTA on Home (nav, hero, the dropdown's "EslothAI Chat" item, every Plan card) now routes to the login gate rather than skipping it.
- **`LoginPage.tsx` is a full rewrite**: no nav bar, a two-column split (`.auth-page`), brand + heading + quote on the left (reusing the same copy as Home's hero, kept consistent rather than inventing new tagline text), a floating `.auth-card` on the right with only the Google button, a plain divider, and legal text. `.login-google-btn` and `.login-legal` were kept (not renamed) since the new page reuses them directly.

### Open

- **Real backend Google OAuth does not exist and is required before this is more than a UI mockup.** Mirroring EngCoach's actual pipeline: a Firebase (or equivalent) project, server-side ID token verification, a real user table (none exists today — the app only has an anonymous `localStorage` UUID), JWT issuance, and JWT-checking middleware on every request. `storage.ts`'s `isLoggedIn()`/`setLoggedIn()` and the `TEST_AUTH_TOKEN` dev value are the two places to replace once that exists; every call site already only depends on "is there a non-empty token," so replacing the value with a real JWT needs no changes elsewhere.
- Hero image is still a placeholder; needs a real asset from the user.
- Platform/Solutions/Pricing/Resources/Contact sales have no destination and are disabled. If any of these become real pages, this is where to wire them.
- **The three pricing tiers are fictional.** No billing, quota, or tier-gating system exists in this codebase. If real billing is ever built, the Plans section's copy, prices, and CTAs all need to be reconciled against whatever the real product actually enforces — right now they're aspirational marketing text only, consistent with every other CTA on this UI-only page.

## MTRAG Benchmark for the Retrieval Chain

Status: complete for retrieval scoring; the rewriter variant is wired conceptually but not yet implemented.

### Discussed and decided

- Requested: benchmark the current retrieval approach per provider. The existing `retrieval.py` could not answer that. It calls `mteb.get_model(name)`, which loads a local Hugging Face encoder, while our providers are HTTP APIs behind a different contract, and MTEB builds its own flat index rather than exercising our chain.
- Checked availability before proposing anything, because the previous round had recommended HiCBench and MTRAG without verifying either. **MTRAG is real and reachable**: downloaded a 45.5 MB corpus file, confirmed Apache-2.0. **HiCBench is not**: the paper points at `huggingface.co/datasets/Youtu-RAG/HiCBench`, which 401s, and the org has no public datasets. Calibrated that check by requesting a deliberately fake repo, which also 401s, so the code means "not there" rather than "log in". The model and the source corpus are public; the manual annotations, the part that matters, are not.
- **Dropped the MTEB `SearchProtocol` route entirely** once the MTRAG README showed BEIR format. BEIR is `corpus.jsonl` / `queries.jsonl` / `qrels.tsv`, which needs no MTEB, no torch, and no protocol wrapper. Two loaders and two metric functions replaced a dependency that installs a deep-learning stack.
- Chose `passage_level/` corpora over `document_level/` after checking rather than assuming: qrels ids carry chunk offsets (`10171-0-2129`), and 479 of 479 distinct FiQA ids resolve directly against the passage corpus.
- Each passage is indexed as one citable leaf with `chunk_id == passage_id`. Anything else scores every hit as a miss, since the qrels reference passage ids. The honest consequence is written into the module docstring and the README: with a flat corpus, Q0 routing, Q4 auto-merge and Q5 definition pinning have nothing to act on, so this measures Q1 plus Q3, not the whole chain.
- Sampling keeps every judged passage. Dropping one would make its query unanswerable and depress recall for a reason unrelated to retrieval, so `--max-passages` fills the remainder at random and the README states that sampled runs are not comparable to the published table.
- Metrics were written here rather than taken from `pytrec_eval` (C toolchain on Windows) or `mteb` (torch). Graded relevance uses the standard exponential gain, because MTRAG qrels carry scores above 1 and its published table was computed that way.

### Implemented

```
mtrag_data.py     fetch(), read_corpus/read_queries/read_qrels, marker stripping
mtrag_metrics.py  recall_at_k, ndcg_at_k, evaluate
mtrag_run.py      passages_to_chunks, sample_passages, TokenBudget, _retry,
                  index_corpus, provider bundle built without the app factory
README.md         two runners, which to use, rate limits, how to read the numbers
.gitignore        the 103 MB data directory
```

### Verified

- 32 new tests; 453 in the suite.
- Data: 366,479 passages, 777 queries, qrels resolving 4/4 domains.
- One live run, fiqa / cohere / 2,000 passages / 20 queries: `lastturn` nDCG@10 0.631, human `rewrite` nDCG@10 0.720. The rewrite gap is the direction and rough magnitude MTRAG's own baselines show.
- Ran with `--in-memory` throughout, because the host's Qdrant was down. That was left alone rather than started: restarting a service the user runs themselves is outside the request.

### Found while building, not requested

- **`CohereEmbedder` sends every chunk in one request.** Cohere caps an embed call at 96 texts, so a document producing more than 96 chunks fails upload with a sanitized 503. A ~100-page PDF reaches that. `VoyageEmbedder` and `JinaEmbedder` have the same shape and untested limits.
- **No adapter retries a 429.** Trial keys cap at 100,000 tokens/minute; the benchmark hit it after ten batches in fifteen seconds. In the app this surfaces as a failed upload rather than a wait.
- **Importing `rag_ingestion.api.app` builds the whole application.** `app = create_runtime_app()` runs at module scope, so any script importing a helper from there opens Postgres, creates tables and connects to Qdrant as a side effect.

All three are reported rather than fixed: batching and retry belong in the three adapters with their own limits and tests, and moving the app construction is an API-surface change. None of them is caused by this work.

### Open

- The fourth variant, scoring `UnderstandMessage`'s own rewrite against the same qrels, is the reason this benchmark was chosen and is not built yet. It needs an OpenRouter call per query inside the loop and its own pacing.
- Only Cohere has been run. Voyage and Jina are wired and untried.
- Full-corpus numbers do not exist yet; everything measured so far is on a 2,000-passage sample.

## Third Hardcode Audit: the Knobs That Actually Matter

Status: complete.

### What the audit found

Asked for a third time whether anything else was hardcoded, so the sweep widened beyond keyword patterns and module constants to endpoints, timeouts, and — the one that mattered — settings that exist but are never passed.

**The finding inverted the expectation.** Two config objects were fully formed and reachable, and neither was ever supplied:

- `create_query_app(retrieval_config=...)` existed; `app.py` never passed it.
- `chunk_document(config=...)` existed; the upload route never passed it.

So `max_candidates`, `token_budget`, `leaf_target_tokens` — the numbers that move retrieval quality most — were fixed at their defaults in production, while trivia like the understand-context character budget had just been made tunable the round before. It also blocked the benchmark plan directly: a sweep over chunk size or candidate depth was impossible without editing source between runs.

### The cycle that had to be broken first

`config.py` imported `providers.keys`. Importing that submodule runs `providers/__init__`, which reaches `adapters` → `qdrant_index` → `chunking`. Making the chunker import `config` would have closed the loop.

Fixed by making `config` a true leaf: `parse_keys` moved into it (it parses one environment string, so config is its correct home) and is re-exported from `providers.keys`; `RetrievalConfig` and `ChunkingConfig` moved in and are re-exported from `retrieval` and `chunking`. Every existing import path still resolves, verified before touching anything else.

### A validator subtlety worth recording

Converting `RetrievalConfig` from a frozen dataclass to a `StrictModel`, the obvious move is `gt=0` on every integer. That silently broke the `global_k` guard: a field constraint runs **before** the model validator, so the explanatory message ("must stay positive to keep a global fallback") was replaced by pydantic's generic bounds text. An existing test asserting on that wording caught it. `global_k` now deliberately carries no field constraint so the validator can own the explanation — noted in a comment so it does not get "fixed" later.

### Implemented

```
config.py             parse_keys moved in; ChunkingConfig + RetrievalConfig
                       moved in; base URLs, timeouts, per-node token ceilings,
                       per-scope memory caps added; decimal() reader for floats
providers/keys.py     re-exports parse_keys
retrieval/chain.py    imports RetrievalConfig from config
chunking/chunker.py   imports ChunkingConfig from config
providers/adapters.py base_url + timeout are constructor arguments
providers/openrouter.py, ingestion/parsers/datalab.py   same
storage/pg_memory.py  caps are a constructor argument; stale docstring fixed
api/app.py            passes retrieval, chunking, URLs, timeouts, token
                       ceilings and memory caps; LLMAnswerer now takes the
                       configured generation ceiling
api/routes.py         threads ChunkingConfig down to chunk_document
compose.yaml          26 new passthrough variables
```

### Verified

453 tests pass, 95% coverage, `config.py` 99%.

The tests deliberately prove the wiring reaches **behaviour**, not just that a setting parses — parsing was never the broken part:

- `retrieval_config=RetrievalConfig(max_candidates=1)` makes the reranker observe exactly one candidate, with a negative control asserting the default run observes more than one.
- A deliberately tiny `ChunkingConfig` produces more chunks from the same upload than the default configuration does.

Every environment variable the config reads was checked against `compose.yaml` programmatically (66 read, 0 missing) rather than by eye.

### Deliberately still hardcoded

- `structure.MIN_UNIT_MATCHES` / `MIN_COVERAGE` / `MAX_SINGLE_UNIT_SHARE` — thresholds that decide whether structure detection is *correct*. A config that lets someone set `MIN_COVERAGE=0` breaks detection silently rather than tuning it.
- `_UPLOAD_CHUNK_SIZE` — a 1 MiB streaming buffer, no operational reason to vary.
- `PAGE_RENDER_DPI` and `understanding._MEMORY_CAP` — unchanged from the previous round's reasoning (plumbing cost for the first, schema coupling for the second).
- The base URLs still appear as **function-signature defaults** in the adapters. That is the acceptable form: a caller overrides them, and `app.py` does from config. They are no longer the only path.

### Open

- `MEMORY_CAPS` in `storage/memory.py` remains the fallback default for any `PostgresMemoryStore` built without explicit caps, which is every test. Only `api.app` passes the configured values, so the module constant is still the effective value in-process for direct store users.
- The English prompt scaffolding and the writing-system assumptions (sentence splitting on `.!?`, tokenizer covering CJK but not Thai/Lao/Khmer/Burmese) are still open. They are not config problems, which is why they were explained rather than moved.

## Config Sub-models, and a Smaller Window for UnderstandMessage

Status: complete.

### Discussed and decided

Four proposals were put up for evaluation. I agreed with three and pushed back on one:

- **"6 → 3 turns for U": rejected as stated, countered with 4.** `understand_context` slices `messages[-N:]` where each element is one message, not a pair, so an odd cap yields `(assistant, user, assistant)` — an answer separated from the question that produced it. Design §3.3 calls the pair the meaningful unit. The user accepted 4. Worth recording that a count cannot guarantee pairing in general either: a failed generation persists the user message with no assistant reply, so two user messages can sit adjacent. Four is right in the normal case, not a guarantee.
- **Thread summary into U's context: agreed**, and it is what makes the smaller window safe rather than pure loss — the summary is the only thing holding what scrolled out. Flagged at the time that it crosses design §3.4's "context is never generated text", and shipped with two mitigations: its own budget (so it cannot evict the recent turns, which are the stronger signal) and an explicit `may be lossy` label (so a detail in it is not read as a quote).
- **Constants into config: agreed, but not all of them.** Taken absolutely the rule would push algorithm invariants into config, which is worse. The line drawn: a value belongs in config when an operator has a real reason to change it per deployment.
- **Sub-model grouping: agreed**, with a measured justification rather than an aesthetic one.

### Why grouping by consumer

The argument that settled the shape: `create_query_app` had **22 parameters**, eight of which were loose integers that existed only because config was flat. Grouping by *consumer* collapses those into two objects; grouping by *topic* would have produced classes that no caller ever passes around whole, which is ceremony. Each sub-model now has exactly one component that takes all of it.

### Implemented

```
config.py            ProviderConfig, OcrConfig, LLMConfig, ConversationConfig,
                      UploadConfig, LoggingConfig; RuntimeConfig keeps only the
                      infrastructure endpoints and the six sub-models.
                      from_environment builds them nested; env var names unchanged.
api/routes.py         create_query_app takes conversation= and upload=; 8 manual
                      positive-value guards deleted (now `gt=0` on the fields);
                      understand_context takes the summary and applies a second
                      budget; 4 constants removed from the module
api/app.py            reads the nested config throughout
retrieval/answering.py  build_turns(memory_char_budget=...)
memory_routing.py     should_promote(threshold=...)
compose.yaml          6 new passthrough vars
```

### Verified

414 tests pass, 95% coverage, `config.py` 100%. `docker compose config` valid.

The new-behaviour tests were checked for the failure mode found in the previous round (tests that pass for the wrong reason): the 4-message test was re-run with the default flipped back to 6 and **fails**, so it genuinely distinguishes the two values rather than asserting something that was already true.

One self-inflicted bug worth recording: the bulk identifier rewrite that moved `max_upload_bytes` onto the config object also rewrote it inside the *parameter list* of `_process_one_upload`, a module-level helper with no access to the closure. Caught by a `SyntaxError` at collection. A regex with a negative lookahead for `=` does not exclude an annotated parameter (`name: int,`); the safer shape is to restrict the rewrite to a known line range or to parse rather than substitute.

### Open

- **`PAGE_RENDER_DPI` was named as config-worthy during evaluation and then left alone.** Making it configurable means threading a value through `process_document → parse_pdf → render_pages` for a number that is already an overridable function default with no realistic operator demand. The rule is "operators have a reason to change it"; this one does not clear that bar once the plumbing cost is visible.
- **`understanding._MEMORY_CAP` likewise stays put.** It is coupled to `Understanding.memory`'s `max_length=3`; making the cap configurable means removing that schema constraint, and design §6.9 puts the cap in the schema deliberately.
- **Summary-in-U is a live deviation from §3.4-1.** If rewrite quality drops, this is the first thing to switch off — `UNDERSTAND_SUMMARY_CHARS=0` disables it without touching the summary itself, which is why the field allows zero.
- The two changes shipped together in one round, so a change in rewrite quality cannot be attributed to the smaller window versus the added summary without the ablation harness discussed earlier.

## Removing Hardcoded Language Patterns from Retrieval

Status: complete.

### Discussed and decided

- Triggered by the user asking what `references.py`'s patterns were actually for, then setting the rule explicitly: hardcoding is only permissible if it earns its keep, hardcoding for a small number of cases is not allowed, and the product must be usable by anyone.
- The capability was worth defending and I said so before agreeing: following a cross-reference is something vector search *structurally cannot* do, because the citing chunk and the cited clause can share no vocabulary. So the question was never "delete the feature", it was "delete the hardcoding".
- What settled it: **§3.1 of `2.multi-turn-memory.md` had already banned exactly this**, categorically — *"Không còn danh sách từ theo ngôn ngữ ở bất cứ đâu trong thiết kế."* `references.py` predated the rule and was never audited against it. The user's instinct and their own written design agreed.
- Generality audit, which is what made the decision easy: the patterns cover ~3 languages (Spanish `Artículo`, Japanese `第5条`, Chinese, Korean, Russian, Arabic all fail) and exactly one domain. Every non-legal document — medical, engineering, HR, research — gets nothing. For a general product that is the majority of the corpus, not an edge case.
- `_DEFINITION_PATTERNS` was the worst of them: `là` is the most common copula in Vietnamese, and the guard at line 125 (drop terms over 8 words outside a glossary section) existed precisely because the pattern over-fired. The guard was the admission.
- Comparison that shaped the replacement: `structure.py` solves the same class of problem correctly — general paths first (`section_path`, `heading_block` from the document's own markup), regex only as fallback, a language-neutral `dotted` pattern, and `_passes_coverage_test` validating before trusting. `references.py` had none of that: regex was the only path, any match was trusted, nothing was reported on failure.
- Two scope questions were put to the user rather than decided unilaterally, since both were architectural: whether Q5 should be deleted or left as an empty shell (chose delete — no stubs), and whether `structure.py`'s keyword patterns were in scope this round (chose yes, accepting the stated cost).

### Checked before cutting

- **`Chunk.refs` does not die with the linker.** It is built from *all* block relations with no type filter, and `reconciliation.py` independently emits `CAPTION_OF` and `CONTINUES`. So Q4 keeps working on parser-derived structure. Had I assumed otherwise I would have deleted a working stage.
- **`Chunk.defines` does die.** Its only producer was `extract_glossary`, so Q5 could never fire again. That is what made deleting Q5 the honest option rather than the aggressive one.
- **`fetch_definitions` was already dead** — defined in `qdrant_index.py`, called from nowhere including tests, before any of this work.

### Implemented

```
chunking/references.py       moved to .trash/, not hard-deleted
tests/test_references.py     moved to .trash/
chunking/chunker.py          Chunk.defines field, glossary params and call sites
chunking/structure.py        CLAUSE_PATTERNS trimmed to `dotted` only
chunking/__init__.py         4 exports removed
rag_ingestion/__init__.py    2 exports + stale layout docstring line
indexing/qdrant_index.py     fetch_glossary, fetch_definitions, defines payload
                              field, defines payload index
retrieval/chain.py           Q5 block, definition_cap, definitions_pinned,
                              stale ordering comment
```

### Verified

410 tests pass. Dead-reference sweep over `backend/rag_ingestion/` and `backend/tests/` is clean; frontend has no references either. Import of `rag_ingestion.api.app` succeeds.

Two things worth recording because they were nearly missed:

- After trimming `CLAUSE_PATTERNS`, `test_coverage_test_rejects_non_monotonic_numbering` and `test_coverage_test_rejects_low_coverage_matches` **still passed — for the wrong reason.** With no pattern matching anything, `detect_natural_unit` returned `NONE` before the coverage logic ran, so tests named after that logic stopped exercising it. Rewritten onto `1.1`-style numbering so they test what they claim.
- The reworked Q4 test was checked empirically against a negative control before being trusted: without the relation the target block is absent and `reference_expanded == 0`; with it, present and `1`. The control is now a permanent test so the Q4 test cannot silently decay into asserting that plain search found the block.

Added `test_detection_does_not_depend_on_the_language_of_the_text`: identical numbering in Vietnamese, Japanese, Russian and Spanish must produce identical detection. That is the property the whole change buys, so it gets a regression guard.

### Open

- **`RelationType.DEFINES`** is an unused enum member in `models.py`. Already unused before this change, and part of the public `Block` contract, so removal is a separate decision.
- **Cross-reference following is now gone as a capability.** If it is wanted back, the general form is: learn the document's structural marker from repetition (which `StructureDetection` already computes and records in `starts`/`labels`), then treat a citation as a mention of that same marker elsewhere in the body. No keyword list, works in any script. Not built.
- **Definitions have no general replacement.** `X là Y` is natural-language semantics, exactly the class §3.1 banned. The precedent for solving it is `UnderstandMessage`: one cheap LLM call, here at ingest time so the cost amortizes over every future query against the document rather than per message.
- **Other generality gaps found during the audit, deliberately not touched this round** (different class — writing-system assumptions, not domain guesses): `chunker.py:79` splits sentences on `.!?` and so mis-splits Chinese/Japanese (`。！？`) and Thai (no sentence punctuation); `qdrant_index.py` handles CJK token ranges but not Thai/Lao/Khmer/Burmese, which are also space-free; `retrieval/answering.py` hardcodes English prompt scaffolding (`"this conversation only"`, `"saved N days ago"`), which is localization rather than retrieval.
- **`structure.py` now detects only two-level numbering** (`1.1`), not plain `1.`. Single-level numbering is far more often an ordinary list than a hierarchy, so widening it needs the coverage test tightened first. Pinned by `test_a_plain_numbered_list_is_not_mistaken_for_structure`.

## Documentation Index and the Design/Implementation Boundary

Status: complete.

### Discussed and decided

- Requested: keep feature design docs in `document/`, and restructure the folder to suit that. Measured before proposing anything: 11 docs, 23 cross-references pointing into `document/` (13 at `2.multi-turn-memory.md` alone), one 0-byte file, one 1-line file.
- **Recommended against the folder reorganisation the question implied.** The `N.<name>` / `N.lesson` numbering already pairs each design with the research behind it, and a `design/` + `research/` + `reference/` split would have broken all 23 references to solve a findability problem an index solves for free. Moved nothing.
- **Found the convention already in the repo rather than inventing one.** Three files (`provider-embedding-reranking-answer.md`, `mteb-retrieval-benchmark.md`, `spec-uv-environment-no-default-torch.md`) already carried a full frontmatter block: `title`, `type`, `created`, `status`, `baseline_commit`, `context`. Standardised on that and extended the `type` vocabulary with `design`, `research` and `orientation`, instead of imposing something new on files that already had a working scheme.
- **The real gap was not structure, it was the missing boundary rule.** `document/` and `IMPLEMENT.md` both describe features, and nothing said which gets what. That is not hypothetical: the L2 summary work earlier the same day put "verified live", the measured thread counts, and the open caching limitations into the design doc, duplicating what this log already held. Wrote the rule down (contract vs acceptance record, before vs after) and then applied it by stripping that content back out of §5.1/§5.2.
- `rag.md` was left where it is after reading it. It is a field survey plus the chosen architecture (§9b), so it is the umbrella for the whole folder, not a step in the `rag-workflow/` pipeline series. The README points at it as the first read.
- Retired rather than deleted, per the pre-trash rule: `benchmark.md` and the empty `0.workflow.md` went to `archive/pre-trash-20260806/`. `benchmark.md`'s only content, the BRIGHT dataset URL, was carried into `mteb-retrieval-benchmark.md` with a note on why it is the next benchmark worth wiring up, so retiring the file cost no information.
- The README calls out `2.multi-turn-memory.md` §17 by name as the table most likely to rot, because it already did rot and already caused a wrong status report.

### Implemented

```
document/README.md          index, reading order, frontmatter spec,
                            document/ vs IMPLEMENT.md boundary
document/*.md               frontmatter added to the six files lacking it
mteb-retrieval-benchmark.md "Datasets not wired up yet" (BRIGHT)
2.multi-turn-memory.md      §5.1/§5.2 renamed "Đã build" -> "Chốt";
                            verification and open items removed (they live here)
archive/pre-trash-20260806/ benchmark.md, 0.workflow.md
```

### Verified

All 10 relative links in the README resolve. No file anywhere still references either retired stub. Every one of the 10 docs reports a `type` and a `status`.

### Open

- `IMPLEMENT.md` is 33 entries and ~1400 lines, append-only. `CLAUDE.md` §XXV prescribes splitting at this size (`IMPLEMENT-<subsystem>.md`). Proposed splitting by subsystem rather than by date, since `CHANGELOG.md` already owns the time axis; not done, awaiting a decision.

## Multi-key Round-Robin for Provider API Keys

Status: complete.

### Discussed and decided

- User had already put multiple comma-separated keys in `.env` for Cohere, Voyage, and Jina (separate free-tier accounts, by the look of them) before asking for this — only the first key was ever actually used. Then added a second key each for Datalab and OpenRouter mid-conversation and asked to cover those too.
- Asked directly whether the LLM key should round-robin the same way. Answer: no. Round-robining every request risks landing turn 2 of a conversation on a different account than turn 1, which throws away whatever prompt-cache warmth had built up on the account used earlier — and if the multiple keys are separate accounts rather than one account's spares, a provider-side cache never carries over between them at all. `OpenRouterChat` does not set `cache_control` today, so there's nothing to lose yet, but the design should not become a landmine the day caching is turned on.
- Two strategies: plain round-robin (`KeyRotator`) for the stateless calls (embedding, reranking, OCR) where every call is independent and cycling is pure upside; sticky-by-`thread_id` (`StickyKeyPool`) for the LLM, so one conversation's calls stay on one account while different conversations still spread across the pool. The one LLM call site with no thread (`/projects/{id}/answer`, a stateless one-shot probe) falls back to `project_id`.
- Chose per-request header overrides over one `httpx.Client` per key. A shared client with a mutated default `Authorization` header would race under the upload executor's concurrent worker threads (thread A's request could pick up thread B's header mid-flight); passing `headers=` explicitly on each `client.post(...)` call is thread-safe without opening N connection pools per provider.
- Datalab draws one key per `parse_pages()` call, not per HTTP request inside it — the check URL a submit returns is almost certainly scoped to whichever account created the job, so the poll and result fetch must reuse it rather than drawing again.

### Implemented

```
providers/keys.py (new)      parse_keys, KeyRotator, StickyKeyPool
config.py                    5 fields renamed str|None -> list[str]
                              (cohere/voyage/jina/datalab/openrouter_api_keys)
providers/adapters.py        _post() headers override; _client() drops its
                              default Authorization; every embedder/reranker
                              rotates a fresh header per call
ingestion/parsers/datalab.py DatalabClient.parse_pages(): one key per call,
                              reused across submit + poll + result fetch
chat.py                      ChatCompleter.complete()/.stream() gain
                              sticky_key: str | None = None
providers/openrouter.py      OpenRouterChat holds a StickyKeyPool; resolves
                              sticky_key (or falls back to round-robin) into
                              a per-request header
retrieval/answering.py       Answerer.answer() gains sticky_key (MockAnswerer
                              ignores it, LLMAnswerer forwards it)
understanding.py             Understander.understand() gains sticky_key
summarization.py             Summarizer.summarize() gains sticky_key
api/routes.py                4 call sites wired: understand.understand(),
                              completer.stream(), summarizer.summarize() all
                              get sticky_key=thread_id; the stateless
                              /projects/{id}/answer route gets project_id
api/app.py                   _bundles() and the chat/understander/summarizer
                              construction block pass the key lists straight
                              through instead of a single string
```

### Verified

414 backend tests pass, 95% coverage. No stub markers in changed files.

- `test_keys.py` (new, 14 tests): `parse_keys` parsing edge cases; `KeyRotator` cycling, wraparound, thread-safety under real concurrent threads, empty-list rejection; `StickyKeyPool` same-sticky-same-key, different-stickies-spread, `None`-sticky round-robins, empty-list rejection.
- `test_provider_adapters.py`: rotation across two keys for both an embedder and a reranker, verified via the actual `Authorization` header sent to a mock transport; empty-key-list rejection.
- `test_pdf_parser.py`: a job's submit and its poll land on the same key; two separate `parse_pages()` calls alternate; empty-key-list rejection.
- `test_openrouter.py`: same sticky key always resolves the same account across repeated calls (`complete` and `stream` both); different sticky keys spread across the pool; no sticky key round-robins; empty-key-list rejection.
- `test_config.py`: comma-separated parsing for all five fields in one request, including a blank entry in the middle being dropped.
- `test_app.py`, `test_thread_api.py`: mechanical updates everywhere a `RuntimeConfig`/fake chat completer/fake understander/fake summarizer was constructed with the old singular shape; one new test confirming `understand_model` and `openrouter_model` can point different `OpenRouterChat` instances at different models independently of the key list.

### Open

- No live inspection of which key an actual request used against the real providers — confirmed through the unit tests' mock-transport header assertions instead, since there's no way to read a header from outside the process once it's sent.
- `GROQ_API_KEY` exists in `.env` but is not wired to anything in this codebase; left untouched, out of scope.

## L2 Thread Summary and the Cacheable Prompt

Item 14 of `document/rag-workflow/2.multi-turn-memory.md` §17, plus a prompt restructure agreed in the same conversation. Status: complete.

### Discussed and decided

- Measured before building, because §5 of the design sets its own threshold ("đo `thread_turn_count`, thấy đuôi thật vượt 30 lượt thì mới làm"). Live Postgres held 8 threads, longest 8 messages, average 4.1, none over the 40-message cap: nothing was being truncated, so L2 would be code that never fires. Reported that to the user before starting; they chose to build it anyway, which is a legitimate call for a feature whose absence is invisible until a thread suddenly gets long.
- **Incremental over full re-summarization.** The watermark (`summarized_through_position`) makes each round fold the previous summary plus only the newly-dropped turns. Full re-summarization stays consistent but its cost grows with thread length; incremental risks compounding error but is flat. Chose flat, matching MemoryOS's FIFO tiering, which the design cites.
- **The user asked whether the system prompt survives summarizing.** It does, by construction, and that was worth confirming rather than asserting: `thread_messages` only ever stores `USER` and `ASSISTANT` rows, `history_turns` reads only from there, and `build_turns` rebuilds the system message from scratch at index 0 every turn. The summary lands in that same system message, so it is equally safe.
- **Evidence moved to the final user turn.** The user's instinct was that this would grow context faster. Checked the arithmetic rather than agreeing: evidence appears exactly once per request either way and the DB stores only the raw question, so it is token-neutral, purely a reposition. The growth they were picturing is real but only under a design we explicitly rejected, namely persisting the evidence-laden message so history matches the request byte-for-byte to reach a 100% cache hit. Researched current practice to check the placement itself: task-varying context belongs in the user turn, and lost-in-the-middle work (U-shaped attention, >30% degradation for middle positions) makes the end of the request the better slot regardless of caching.
- **One rule set instead of plain/grounded.** Discovered while planning the caching change: the two rule variants were selected per turn by whether retrieval returned anything, so one empty retrieval mid-thread rewrote the system message. Unifying them is what actually makes the prefix stable; moving evidence alone would not have been enough.
- **Told the user the caching win is partial**, rather than letting the restructure imply otherwise. The cacheable prefix ends at the previous exchange, because history replays the bare question where the request carried evidence plus question, and evidence is never cacheable at any position. Also flagged that L2 caps history growth and therefore caps the very prefix that caching would have paid off on, so the two features partly overlap.
- Summarizing runs after the `done` event and swallows its own failures. A provider error returns the previous summary rather than `None`: clearing it would silently discard conversation history, which is worse than a summary one round stale.
- `set_summary` deliberately leaves `sequence` and `updated_at` alone, following the precedent `rename_thread` set. It runs after the answer is sent, so it is bookkeeping, not activity, and must not reorder the sidebar for something the user never did.

### Implemented

```
summarization.py            Summarizer protocol, LLMSummarizer, silent fallback
prompts/summarization.py    SUMMARY_SYSTEM_PROMPT + two user templates
db/tables.py                ThreadRow.summary, .summarized_through_position
migrations/…801f5573326a    the two columns
storage/threads.py          Thread fields + ThreadStore.set_summary
storage/pg_threads.py       set_summary, summary read back in _thread
prompts/answering.py        SYSTEM_PROMPT (one rule set), EVIDENCE_BLOCK,
                             section templates lifted out of the renderer
retrieval/answering.py      build_turns(summary=...), evidence to the user turn
api/routes.py               window(), history_turns() rebuilt on it,
                             refresh_summary(), four settings as parameters
api/app.py                  LLMSummarizer wiring + startup line
config.py                   five new settings
compose.yaml                the five passthroughs
```

Defaults: keep the first turn plus the last 6, summarize once 8 messages sit outside that window, outer caps unchanged at 40 messages / 24k characters.

### Verified

- 388 tests, 95% coverage. `summarization.py`, `retrieval/answering.py`, `config.py`, `storage/threads.py` all at 100%.
- 21 new tests. The load-bearing ones: the second summarization shares no user message with the first (proves the watermark), the system message is byte-identical with and without evidence (proves the cache precondition), and the middle leaves the replayed history while the opening turn stays.
- Six existing tests encoded the old contract (evidence in the system message, two rule sets) and were rewritten to the new one rather than deleted.
- Two of my own new assertions were wrong on first run and were fixed rather than worked around: one checked for the literal string `"## Evidence"` in the system message, which the rules legitimately contain so the model can recognize the marker; the other compared assistant replies, which the test fake makes identical every turn.
- Live against the rebuilt container: startup shows `summary=openrouter/free`; an 8-turn thread produced a correct, coherent summary with the watermark at 10 of 16 messages; the following turn's logged payload showed the summary section in the system message, the first turn plus last 6 replayed, and the bare question last. Probe thread deleted.
- `docker compose config --quiet` passes. Stub-marker scan over all ten changed source files: no matches.

### Open

- The cacheable prefix stops one exchange short of the newest turn, and evidence is never cacheable. Closing that gap would mean persisting evidence into history, which was explicitly rejected.
- No provider in this stack sets `cache_control` yet, and `OpenRouterChat._payload` still sends only `model`/`max_tokens`/`stream`/`messages`. The restructure makes the prompt cacheable in shape; actually earning hits needs a model that supports it. `usage.prompt_tokens_details.cached_tokens` is already logged and is how to check.
- Summarization is synchronous inside the SSE generator after `done`. The client has its answer, but the server holds the request open for one more LLM call.

## System Prompt Rules for Markdown, LaTeX, and Mermaid

Status: complete. Follow-up to the frontend rendering work above: that shipped with no backend change, so the model had no reason to actually use the syntax the frontend now knows how to render.

### Discussed and decided

- Rules drafted first as a plain answer to "give me a prompt", reviewed with the user, then applied on explicit request rather than assumed.
- Delimiters had to match `remark-math` exactly (`$...$`, `$$...$$`), not the equally-common `\(\)`/`\[\]` LaTeX convention, since the frontend only parses the former. Said so explicitly in the rule rather than leaving it to the model's own default.
- The mermaid rule states the fence tag requirement as load-bearing ("this exact tag is required"), not a style suggestion, because it is: `Message.tsx`'s `pre` override matches on `language === 'mermaid'` verbatim.
- Added a rule against wrapping the whole answer in one outer code fence based on direct observation, not speculation: this failure mode showed up repeatedly while testing the rendering feature itself, where the model answered "show me a table" by fencing the entire reply as one `​```markdown` block, so nothing inside it rendered.
- Added a rule against raw HTML and images because the frontend would silently no-op on both (`react-markdown` doesn't render raw HTML without `rehype-raw`, which isn't installed, and the model has no real image URL to give) — worse than not mentioning them, since the model would emit dead syntax with no error to notice.
- Factored the six bullets into one `_FORMATTING_RULES` constant rather than duplicating them, since at the moment of writing there were two prompt variants (plain/grounded) needing identical text. A concurrent, unrelated change (below) later collapsed those two into one `SYSTEM_PROMPT`; the shared constant meant that collapse needed no rework on this rule set's part.

### Implemented

```
prompts/answering.py       _FORMATTING_RULES, appended onto SYSTEM_PROMPT
tests/test_llm_answerer.py test_formatting_rules_are_shared_by_both_modes_...
```

### Landed alongside a concurrent backend change

While this was in progress, another change unified the plain/grounded prompt split into one `SYSTEM_PROMPT` (moving evidence to the final user turn so the system message stays byte-identical across turns and cacheable) and started on a separate conversation-summarization feature (`LLMSummarizer`). Two `docker compose build api` attempts each caught that other work mid-edit — first `retrieval/answering.py` not yet updated for the prompt rename, then `LLMSummarizer` referenced in `app.py` before its own module was finished — neither related to this change. Not chased further: rebuilding into a moving target repeats the same lesson from the batch-upload collision earlier this session. Verified through the test suite and a captured prompt render instead of a live container.

### Verified

- `tests/test_llm_answerer.py`: 25 passed, including the new test.
- Full backend suite: 376 passed. Two failures in `test_thread_api.py` (evidence-placement expectations not yet updated for the concurrent prompt-unification change) are pre-existing and unrelated; not fixed, not mine.
- Read the actual system prompt captured in a test's own debug-log output (`LLM request for thread=... system_chars=1508`): all six rules present, worded as written, correctly positioned inside `## Rules`.
- Not verified: a real model response under the new rules. Blocked by the concurrent work leaving the container unbuildable at every attempt during this session; worth a quick live check once that settles.

## Markdown, LaTeX, and Mermaid Rendering in Chat

Status: complete (frontend only, by request; the user is adding the system-prompt side themselves).

### Discussed and decided

- Scoped to the frontend only per explicit request: no change to what the API sends or to the system prompt telling the model it may use this syntax.
- Standard combo for this stack: `react-markdown` + `remark-gfm` for markdown/tables, `remark-math` + `rehype-katex` for LaTeX, `mermaid` behind a custom code-block interceptor for diagrams. No realistic alternative to adding these as dependencies; hand-rolling any of the three is not a serious option.
- User messages and error messages stay plain text. Only a successful assistant message goes through the markdown pipeline — matches the existing `isError` branch, which already skips it.
- Mermaid needs to know whether the message is still streaming, since a diagram fence is nothing until its closing ` ``` ` arrives. Added a `streaming` prop to `Message`, true only on the one bubble in `App.tsx` bound to the live SSE buffer; everywhere else (persisted messages, the error path) it defaults to false. While streaming, a mermaid block renders as a plain code block; once done, it swaps to the real diagram.
- Caught and fixed before shipping, not after: importing `mermaid` at the top of `Message.tsx` put its entire diagram-layout dependency graph in the main bundle unconditionally, since every chat message imports that module regardless of whether it contains a diagram. `npm run build` showed the actual cost (main chunk 1.3 MB) before this was fixed, not just a bundle-size complaint. Converted the `Mermaid` import to `React.lazy` + `Suspense`, which restored the main chunk to roughly its previous size and pushed the diagram-rendering weight onto its own chunk that only loads when a message actually has one.
- KaTeX stays eager (78 KB gzip) rather than also being made lazy: `rehype-katex` is a synchronous rehype plugin handed to `react-markdown`'s synchronous `Markdown` component, and there's no per-message signal to gate on before the plugin list is constructed short of switching to the async `MarkdownHooks` variant and pre-scanning text for `$`. That's a real, bigger option, not pursued here since it wasn't asked for and 78 KB gzip is a normal cost for shipping LaTeX rendering at all.
- `mermaid.initialize({ securityLevel: 'strict' })` set explicitly rather than left to the library default, since diagram source in this app can come from an LLM or, transitively, from a summarized document — not content the frontend should treat as fully trusted. `Mermaid.tsx`'s own `dangerouslySetInnerHTML` is safe specifically because of this setting, not despite it; the comment in that file says so.

### Implemented

```
components/Mermaid.tsx     new: renders a mermaid code string to sanitized SVG,
                            loading/error states, unique id via useId()
components/Message.tsx     ReactMarkdown wired with remark-gfm/remark-math/
                            rehype-katex; pre/code overrides route ```mermaid
                            fences to a lazy-loaded <Mermaid>, gated on the new
                            streaming prop; katex CSS imported here
App.tsx                    streaming prop passed on the one live SSE bubble
styles/global.css          .markdown-body (headings, lists, tables,
                            blockquote, links, code), .mermaid-diagram /
                            -loading / -error, katex-display overflow handling
package.json                + react-markdown, remark-gfm, remark-math,
                            rehype-katex, katex, mermaid
```

### Verified

- `tsc -b` and `vite build` clean.
- Bundle size before/after the lazy-load fix: main chunk 1,316 KB → 678 KB (358 KB → 206 KB gzip); mermaid's own weight (152 KB gzip core + several per-diagram-type chunks, 10-44 KB gzip each) moved from eager to on-demand.
- Live round-trip through the real chat: confirmed code-fence rendering (proper `.md-pre` box, `-->` correctly HTML-escaped inside the block) and plain-paragraph rendering, no console errors.
- The live model (`openrouter/free`) was not a reliable way to exercise table/LaTeX/mermaid rendering specifically: it kept wrapping entire answers in one outer ` ```markdown ` fence rather than emitting the syntax inline, so "did the renderer produce a real `<table>`/KaTeX span/SVG" stayed unproven through the chat alone. Verified deterministically instead: temporarily seeded a fixed assistant message directly in `App.tsx`'s initial state (skipping the model entirely) containing an un-fenced GFM table, `$E=mc^2$`, and a ` ```mermaid ` block. Confirmed in the browser: a real `<table>` with header/body rows, a KaTeX-rendered equation with a proper superscript, and an actual rendered SVG flowchart with three connected nodes, console clean. Reverted the seed immediately after and confirmed via `diff` against the pre-change backup that `App.tsx` returned to exactly its prior state plus the one intended `streaming` prop line.
- Test thread and messages created while manually exercising the real chat during verification were deleted from the backend afterward.

### Open

- The system prompt says nothing about markdown/LaTeX/mermaid being available; the user is adding that themselves. Until then, whether a real answer uses this syntax depends entirely on the model choosing to on its own.
- No syntax highlighting for regular (non-mermaid) code blocks — monospace and background only. Not requested; noting it as the obvious next visual improvement if wanted.
- KaTeX's 78 KB gzip stays in the eager bundle; making it lazy too is possible but requires the async `MarkdownHooks` component and a pre-scan for math syntax, not pursued here.

## uv Environment Without Default Torch

Status: complete.

### Discussed and decided

- Python is pinned to 3.12 and dependencies are locked by `uv`.
- The standard project environment is `uv sync --extra test`. The optional `benchmark` extra is not selected, so MTEB and Torch are absent from `.venv`.
- `uv.lock` resolves every optional extra and therefore contains Torch metadata. Lockfile presence is not installation; selected extras determine what enters the environment.
- MTEB requires Torch, but the project does not choose a hardware build. A developer running benchmarks installs the appropriate CPU or CUDA Torch build first, then synchronizes the locked benchmark dependencies with `--no-install-package torch --inexact`.

### Implemented

```
.python-version                       Python 3.12 project pin
uv.lock                               reproducible base, test, and optional-extra resolution
backend/scripts/benchmark/README.md   default sync and manual Torch benchmark setup
```

### Verified

- Recreated `.venv` with `uv venv --clear --python 3.12` and `uv sync --extra test`.
- `uv lock --check` passes and `uv sync --extra test --check` reports no changes.
- The synchronized base-and-test environment contains 52 packages; isolated Python lookup finds neither `torch` nor `mteb`.
- A benchmark sync dry-run selects 48 locked benchmark packages and excludes Torch from the install plan.

## MTEB Retrieval Benchmark Runner

Status: implementation complete; real MTEB smoke run pending installation.

### Discussed and decided

- Phase 1 measures dense embedding retrieval through MTEB's own task and metric contracts. The application's Qdrant hybrid retrieval and reranker are deliberately a later benchmark because they need a separate adapter and controlled candidate-depth contract; mixing both now would make the first numbers impossible to interpret.
- The five Hugging Face repositories map to MTEB tasks `AILACasedocs`, `AILAStatutes`, `LegalQuAD`, `LegalSummarization`, and `MIRACLRetrievalHardNegatives`. Despite its source name, `legal_summarization` is registered as document retrieval, with summaries acting as queries.
- MIRACL language selection is independent from the fixed-language legal tasks. It defaults to `eng` and accepts only the 18 ISO 639-3 languages published by the task. Running `LegalQuAD` therefore stays German even if MIRACL is scoped to English.
- Model selection is mandatory. There is no hardcoded baseline whose revision or prompt behavior can silently become the project's benchmark truth.
- MTEB remains an optional dependency. Argument parsing and `--help` work without it, while a real run without the extra exits cleanly with the install command.

### Implemented

```
backend/scripts/benchmark/retrieval.py  task registry, argument validation,
                                        MTEB task/model/cache/evaluate wiring
backend/scripts/benchmark/README.md     install, task map, smoke/full commands,
                                        MIRACL size warning and output notes
backend/tests/test_retrieval_benchmark.py
                                        network-free orchestration tests
pyproject.toml                          benchmark optional dependency
.gitignore                              generated result/prediction directories
document/mteb-retrieval-benchmark.md    approved feature contract
```

### Verified

- RED first: focused test collection failed because `scripts.benchmark.retrieval` did not exist.
- Focused tests: 9 passed; new runner coverage: 90.91%.
- Full backend suite: 359 passed; `rag_ingestion` coverage: 94.95% against the 80% gate.
- Real subprocess without the extra: exit code 2, actionable install message, no traceback.
- `--help` succeeds without loading MTEB; VS Code diagnostics and incomplete-marker scan are clean; `pip check` reports no broken requirements.

### Open

- The optional dependency install was cancelled while processing `torch`; `mteb`, `datasets`, `sentence-transformers`, and `torch` are not installed. A real one-task `AILA_statutes` smoke run remains the next step after rerunning `.venv\\Scripts\\python.exe -m pip install -e ".[benchmark]"`.
- This runner does not measure Qdrant hybrid retrieval or reranking. Those need a phase 2 adapter that preserves the same corpus, query, qrels, candidate depth, and metric cutoffs for a fair comparison.

## Redis Query Embedding Cache

Status: complete.

### Discussed and decided

- The immediate scaling problem was three provider embedding calls for a normal project query, or four when a rewritten query is present. Every `hybrid_search` embedded the same text again.
- The first Redis scope is query embeddings because it removes the most duplicated paid operation without changing retrieval ranking. Retrieval-result, glossary, and answer caches remain separate future changes with different invalidation and authorization rules.
- Cache identity includes the complete immutable `EmbeddingProfile` and exact query text. Only its SHA-256 digest appears in the key, so user text is not exposed through key inspection.
- Redis is an optimization, not a dependency of correctness. Any Redis error falls back to the provider. Invalid cached vectors are treated as misses and replaced.
- Document embeddings are not cached. Ingestion batches and index versions have different storage and invalidation concerns, and query duplication was the requested operational issue.
- Redis is off unless `REDIS_URL` is configured. Compose enables it explicitly; ordinary tests and bare host runs do not require a Redis process.

### Implemented

```
providers/embedding_cache.py  RedisCachedEmbedder, hashed keys, vector validation,
                              fail-open reads and writes, document pass-through
config.py                     REDIS_URL and 24-hour embedding cache TTL
api/app.py                    one shared Redis client wrapping configured providers
compose.yaml                  Redis 7, 256 MiB allkeys-lru, localhost host port,
                              healthcheck
pyproject.toml                redis-py production dependency
tests/                        cache behavior, environment parsing, runtime wiring
```

### Verified

- New cache tests were RED before implementation and now pass: one provider call across repeated queries, no raw query in the key, document bypass, dimension/malformed/NaN replacement, and read/write failure fallback.
- 44 tests in the cache, provider adapter, config, and app slice pass.
- Full backend suite: 359 passed, 94.86% coverage against the 80% gate.
- `docker compose config --quiet` passes. The new `rag-redis` service was started independently and returned `PONG`; the API container was not rebuilt or restarted.
- VS Code reports no errors in changed Python or test files.

### Files

- `backend/rag_ingestion/providers/embedding_cache.py` (new)
- `backend/rag_ingestion/config.py`, `backend/rag_ingestion/api/app.py`
- `backend/tests/test_embedding_cache.py`, `test_config.py`, `test_app.py`
- `pyproject.toml`, `compose.yaml`, `CHANGELOG.md`

## Rebrand to EslothAI

Status: complete.

### Discussed and decided

- Requested: rename the brand from "Claude" to "EslothAI" and use the supplied `logo.png`, moved to the correct folder rather than left where it landed.
- `logo.png` was sitting at the repo root. Moved into `frontend/public/`, the Vite convention for a file that must be served byte-for-byte at a fixed root path (`/logo.png`) rather than bundled and hashed like everything under `src/`. That one location covers both the favicon `<link>` and the sidebar `<img>` with no duplicate copies.
- Two touchpoints carried the old name: `index.html`'s `<title>` and the sidebar's `.wordmark` span. Nothing else in `frontend/src` referenced "Claude" as brand text (checked by grep, not assumption).
- The sidebar header changed from bare text to a logo + wordmark pair. The image gets `alt=""`: the adjacent text already reads "EslothAI", so a non-empty alt would just repeat it for a screen reader.

### Implemented

```
logo.png                    repo root -> frontend/public/logo.png
frontend/index.html         <title>EslothAI</title>, favicon <link>
components/Sidebar.tsx      .brand wrapper: logo image + wordmark
styles/global.css           .brand, .brand-logo; .wordmark gained the same
                             overflow/ellipsis handling its sibling labels
                             already use for text in a flex row
```

### Verified

`tsc -b` and `vite build` clean; `dist/logo.png` present after build. Live in the browser: tab title reads "EslothAI", the sidebar shows the logo beside the wordmark at 24px, both `/logo.png` requests (favicon and the sidebar image) resolve, console clean. Grepped `frontend/src` for "Claude" afterward: no matches.

## Test Suite Moved Next to the Package It Tests

Status: complete.

### Discussed and decided

- Requested: move the backend test files out of the repo-root `tests/` into `backend/tests/`. Checked first whether `tests/` held anything else (frontend tests, non-backend fixtures) before treating this as a full-directory move rather than a partial one — every one of its 70 entries (test modules, `conftest.py`, and existing `.backup.*` copies) imports `rag_ingestion.*`, so it was a pure relocation.
- `backend/tests/` already existed, empty, before this request landed.

### Implemented

- `tests/` → `backend/tests/` (plain move, no content changes).
- `pyproject.toml`: `testpaths = ["backend/tests"]`. `pythonpath` untouched.
- `.dockerignore`: the old bare `tests` line stopped matching once the directory moved a level deeper. Confirmed by building the image and finding `backend/tests` inside it — the assumption that a bare name excludes at any depth (true for `.gitignore`, not for Docker's ignore matching) was wrong. Replaced with an explicit `backend/tests` line.

### Verified

308 tests pass from the new location (same count as before). Image rebuilt and inspected directly twice: once showing the leak, once confirming `backend/tests` is excluded after the fix. Did not restart the running `rag-api` container — this changes where files sit on disk, not anything served, so a restart was out of scope for the request.

### Files

- `pyproject.toml`, `.dockerignore`
- `tests/*` → `backend/tests/*` (70 files)
- `CHANGELOG.md`

## Fully automatic memory extraction

### Discussed and decided

- Requested: extraction with no human step. Three options were on the table: write everything straight to memory, keep proposals but flush them with an LLM every N, or something better.
- The batch-of-N option was rejected. A counter of three is not evidence: three candidates on three unrelated keys are no more trustworthy than one, so a bad memory would be flushed because two good unrelated ones happened to arrive. It also delays effect, which is the failure users actually notice: say your name, and the next turn still does not know it. And it adds a second model call plus a scheduler to buy what a static table already gives.
- What needed a gate turned out to be the **scope**, not the write. Thread writes are already safe to automate: capped at ten, gone with the thread, effective immediately. Only global writes leak into every future conversation.
- So the model no longer decides scope. `MemoryCandidate.suggested_scope` was removed after three runs of one sentence returned `user` once and `thread` once. Judging durability is the hardest thing in that prompt and the least reliable output; naming a key and quoting a value is what a cheap model does well.
- The key list stays a closed enum plus `other:<slug>`. The enum is not a limit on what can be remembered, only on which slots get strong supersede.
- `other:*` routes to the thread rather than to a proposal. A probe produced both `other:project` and `other:project_name` for the same sentence, and supersede matches the key exactly, so synonyms coexist forever. The thread contains that, and the two-thread promotion rule will not fire for a synonym, so junk cannot become permanent global noise. This replaced an earlier plan to send `other:*` to proposals, which would have broken the "no human step" requirement.
- Promotion adds a user-scoped row instead of moving the thread's row. Moving it would steal the memory out of the thread that earned it.
- Language sits with the identity keys, not with the repeat-gated ones. Waiting for a second thread means two conversations answered in the wrong language, which is worse than an occasional wrong write that the next message corrects.

### Deviation from the design

§6.5 and item 16 of `document/rag-workflow/2.multi-turn-memory.md` say the same key in two threads should **propose** a promotion, explicitly "không tự nâng". This promotes automatically. That was the point of the request, and the safety net is that every automatic write is visible and deletable in the Personalization tab, with `memory_enabled` as the switch. Automatic plus visible plus reversible is the combination that makes this acceptable; automatic and invisible would not be.

### Implemented

```
memory_routing.py       key -> scope table, unchanged-value guard, promotion threshold
api/routes.py           write_understanding routes each candidate; write_or_propose
schemas/understanding.py  suggested_scope removed; extra keys tolerated
prompts/understanding.py  scope block removed; slot-not-value rule added
storage/pg_memory.py    distinct_thread_count
```

### Verified

344 tests, of which 25 are new: 19 pure routing cases with no database, 3 for the promotion count against Postgres, and new policy tests through the ASGI app covering identity to user scope, situational to thread, `other:*` to thread, promotion on the second thread, a restatement writing nothing, a full scope proposing, and memory disabled dropping everything.

Two existing tests encoded the confirm-first policy and were rewritten to the new one rather than deleted.

Not exercised against the live provider: `openrouter/free` answers HTTP 429, so extraction and generation both fail there at the moment.

### Files

- `backend/rag_ingestion/memory_routing.py` (new)
- `backend/rag_ingestion/api/routes.py`
- `backend/rag_ingestion/schemas/understanding.py`
- `backend/rag_ingestion/prompts/understanding.py`
- `backend/rag_ingestion/storage/{memory,pg_memory}.py`
- `backend/tests/{test_memory_routing,test_thread_api,test_understanding}.py`
- `CHANGELOG.md`

### Open

- The extraction prompt does not yet receive the keys the owner already has, so the model still invents a slug when an equivalent slot exists. `recalled` is loaded before the extraction call, so this costs nothing to pass; it is the main remaining mitigation for slug synonymy.
- `openrouter/free` is unreliable for structured output: 3 of 9 attempts returned unparseable JSON before it began returning 429 outright. A perfect routing table is worth nothing on a turn that produced no candidates. Needs either a model with real JSON mode on `UNDERSTAND_MODEL` or a retry, and switching models is a configuration decision.
- `last_used_at` is still never written, so the 30-row user cap stays first-come-first-served with no eviction signal.
- Candidates dropped inside `_parse` for an invalid key are still silent.

## Memory write logging

### Discussed and decided

- Question asked: does a message like "tôi tên là Wxrdie" rewrite memory, and if so is it logged? Checked by sending exactly that message. `UnderstandMessage` extracted `user/fact/nickname` = "Wxrdie", and it landed in `memory_proposal` with the `memory` table untouched. So the answer is no: nothing is rewritten until the user confirms. Only `suggested_scope=thread` writes straight through, and only that path can supersede unprompted.
- Logging went in `PostgresMemoryStore`, not in the routes. Extraction, the settings UI, and proposal confirm all reach the database through the store, so one set of calls covers every caller; logging at the route would have missed two of the three.
- `ADD` carries the ids it superseded. Supersede is preferred over an in-place overwrite precisely so history is reconstructable, and a line that omits the replaced id gives up that property.
- `PROPOSE` is logged because its absence read as a lost write: the route says "extracted 1 candidate(s)", the `memory` table stays empty, and nothing explained the gap between the two.
- `DELETE` says "marked", not "removed". The row is still there with `deleted_at` set, and a log that says "removed" would send the next reader looking for a row that was never gone.
- Values are never logged. The value is the user's own personal data and the single field that must stay out of a log file, so the lines carry `memory_id` plus `scope/type/key`. The tradeoff is real: to see what was remembered you query the database. A test asserts no write path emits the value even at DEBUG.
- A miss (`update_memory` or `delete_memory` on an id that is not there) logs nothing. Nothing changed, so there is nothing to report, and a line per miss would make a log of failed guesses look like activity.

### Verified

Full lifecycle in one run against a real Postgres over HTTP:

```
UnderstandMessage extracted 1 candidate(s) for thread=thr_42396fce: user/fact/nickname
Memory PROPOSE prop_3419287a suggested_scope=user type=fact key=nickname thread=thr_42396fce
Memory ADD mem_51f47acb user/fact/nickname source=user_confirmed
Memory ADD mem_af1bc3a2 user/fact/nickname source=user_explicit superseded=mem_51f47acb
Memory UPDATE mem_af1bc3a2 user/fact/nickname changed=value
Memory DELETE mem_af1bc3a2 user/fact/nickname marked
```

9 tests in `backend/tests/test_memory_logging.py`, red before the change. 317 pass.

Run from the host venv on a spare port, not in the container: `docker compose build` is currently failing with `error getting credentials ... A specified logon session does not exist`, a Docker Desktop credential-helper problem unrelated to this work. `docker compose up -d --build` restarts the previous image when the build fails rather than erroring, which is how a stale container looked like a missing log line. The running `rag-api` needs Docker Desktop restarted and a rebuild before these lines appear there.

### Files

- `backend/rag_ingestion/storage/pg_memory.py`
- `backend/tests/test_memory_logging.py`
- `CHANGELOG.md`

### Still not logged

- Candidates dropped inside `understanding.py`'s `_parse`: a `ValidationError` or a key outside the closed list is skipped with a bare `continue`. That is the exact failure mode the closed key list creates, and it is silent.
- `write_understanding` returning early because the owner has `memory_enabled` false. The route has already logged "extracted N candidate(s)" by then, so the run looks like a lost write with no explanation. The read path logs this case; the write path does not.

Both are in files another session is editing, so they were left alone rather than risking a collision.

## UnderstandMessage (query rewrite + memory extraction)

Item 11 of `document/rag-workflow/2.multi-turn-memory.md` §17. Status: complete for the scope discussed below; `TurnScorer` ("G") is a separate, still-open item.

### Discussed and decided

- Requested as "add logging for query rewrite." That feature did not exist in code yet — it was `document/rag-workflow/2.multi-turn-memory.md` §3.4 (`UnderstandMessage`), marked open in this log's "Memory Storage and the Candidate Budget" entry. Confirmed direction with the user: build the real node, not a log line pointed at nothing.
- Scoped down from the full design: `TurnScorer` (§3.2-3.3, cosine + z-score selection over embedded history) needs a migration (`thread_messages.embedding` / `embedding_profile`, §8.2) and a vector computed on every message write — a separate subsystem the user did not ask for. This round's U uses the last 6 raw turns / 1500 characters instead, and assistant turns use their stored generated text rather than the design's "cited chunk only" grounding rule (§3.4 rule 1), because grounding that properly needs G to pick which chunk. Both are open items below, flagged rather than silently dropped.
- No real JSON-mode exists in `providers/openrouter.py` (`_payload` sends only `model`/`max_tokens`/`stream`/`messages`). Rather than extend the shared `ChatCompleter` contract for one caller, `LLMUnderstander` prompts for raw JSON and parses defensively, exactly matching the design's own fallback contract in §10 ("lỗi/timeout → Understanding(None, [])").
- U runs for every message, plain chat or project thread (§3.1, §7), gated only on the OpenRouter API key existing — independent of `USE_MOCK_ANSWERER`, because memory extraction must keep working when generation itself is mocked.
- The rewrite is retrieval-only. The original message is always what is persisted and what reaches the answerer; `run_retrieval` never lets a rewrite feed the global fallback source, so a bad rewrite cannot take the safety net down with it (§3.9).
- `suggested_scope=thread` writes immediately; `suggested_scope=user` becomes a proposal. `PostgresMemoryStore.add_proposal` already existed (built for the confirm route) but was never on the `MemoryStore` protocol, so this is also what finally makes `POST /memory/proposals/{id}/confirm` reachable from a live extraction instead of only from a manually-seeded row.

### Implemented

```
understanding.py            Understander protocol, LLMUnderstander, JSON parsing
                             and per-candidate validation
schemas/understanding.py    Understanding, MemoryCandidate (Literal-typed to
                             exclude pin and project scope at the schema level)
prompts/understanding.py    UNDERSTAND_SYSTEM_PROMPT
storage/memory.py           MemoryStore.add_proposal declared on the protocol
retrieval/chain.py          run_retrieval(..., rewritten_query=None): third
                             scoped(rewrite) source, rerank on the rewrite,
                             rewrite_used diagnostic
config.py                   understand_model (env UNDERSTAND_MODEL)
api/app.py                  second OpenRouterChat + LLMUnderstander, gated on
                             the API key alone
api/routes.py               understand_context(), write_understanding(),
                             wired into create_thread_message before the
                             project/plain-chat branch
compose.yaml                UNDERSTAND_MODEL passthrough
```

### Verified

308 backend tests pass, 95% coverage, `understanding.py` itself at 100%.

- `test_understanding.py` (new, 14 tests): valid round-trip, markdown-fenced JSON, malformed JSON, non-object JSON, non-string rewrite, non-list memory, provider error, `pin` type and invalid key dropped per-candidate rather than failing the whole response, memory capped at 3, schema rejects `pin` and `project` scope directly via `Literal`.
- `test_retrieval_chain.py` (6 new): rewrite drives the rerank text, no-rewrite path unchanged, `rewrite_used` diagnostic, and — the one that matters — the exact global-fallback-starvation regression repeated with three active sources instead of two, since adding a source is exactly the change `_fill_pool`'s own docstring warned would need this check.
- `test_config.py`, `test_app.py`: `UNDERSTAND_MODEL` resolves independently of `OPENROUTER_MODEL`; `app.state.understand` wired correctly, including staying wired when `USE_MOCK_ANSWERER` is set.
- `test_thread_api.py` (4 new): a thread-scope candidate becomes a readable memory immediately; a user-scope candidate becomes a proposal and is *not* written to memory directly; a rewrite reaches retrieval (`diagnostics.rewrite_used == 1`) while the persisted message and the text sent to the chat provider stay the original; `understand=None` leaves behavior identical to before this round (the rest of the pre-existing suite, unmodified, is the wider regression guard for that).
- Stub-marker scan (`TODO`/`FIXME`/`HACK`/`PLACEHOLDER`/etc.) over every changed file: no matches.

### Open

- **`TurnScorer` ("G")** is not built. U's context is the last 6 raw turns, not G's cosine/z-score selection — fine for the short threads this product sees today (§5 estimates 5-20 turns), but not what the design specifies.
- **Citation-grounded context (§3.4 rule 1)** is not built. Assistant turns in U's context are the stored generated text, not the cited chunk text, because grounding that correctly needs G to pick which chunk. In this codebase specifically the generated text is itself always produced from real retrieved evidence (`LLMAnswerer`/`MockAnswerer` both cite real chunks), which softens the risk this rule guards against, but it is not the same guarantee.
- **U runs sequentially before retrieval**, not in parallel with it as §3.8 specifies. Plain chat therefore pays one extra LLM round trip per turn that the design intended to hide. Parallelizing would mean restructuring `run_retrieval` to accept a source that arrives after Q0/Q1(original) have already started, which is a real refactor on its own.
- **No dedup** against memory already surfaced in the same thread (§6.13) or against an existing unconfirmed proposal with the same key — every qualifying message can produce a fresh proposal.
- **`last_used_at` is still never written** (carried over from the "Memory Storage" entry; U's writes don't change this).
- Promotion on a key seen in ≥2 threads (item 12) and the L2 thread summary (item 14) remain open, unrelated to this round.

## Corrupt PDF Left a File Stuck at "processing" Forever

Status: complete. Found while verifying the batch upload feature below end to end in the browser, not requested on its own.

### Discussed and decided

- The request this turn was the frontend spinner/checkmark UI for a project's files rail. That UI was built first (`PendingUpload` state, `RailFileRow`, immediate navigation to the project screen on create), then the batch-upload-over-SSE work below landed concurrently and wired it to a real backend endpoint instead of the client-side sequential-loop simulation it started as.
- Verifying that combination live, a genuinely corrupt `.pdf` uploaded through it produced a spinner that never resolved. Traced rather than patched around: `fitz.open(path)` on a malformed file raises `pymupdf`'s own `FzErrorBase`/`FileDataError` family, which `process_upload`'s except clause does not catch (`BadZipFile`, `KeyError`, `OSError`, `ValueError`). The request 500'd, and because it never reached the code that calls `update_file_status(FAILED, ...)`, the file's row stayed at `processing` in Postgres permanently. The frontend rail was correctly displaying a broken backend state, not misbehaving on its own.
- Root cause fixed at the parser boundary (`pdf.py`), not by widening the route's except tuple: the route already has the right shape (catch a known bad-input type, mark the file failed, return 422), it just never learned pymupdf's own exception family. Translating once, where the only `fitz.open` calls are, keeps that knowledge out of the API layer.
- Switched to opening from bytes rather than by path in the same function, not only for the exception translation. Reproducing the fix under this project's actual test suite (Windows host, pytest, no Docker) surfaced a second, unrelated failure: mupdf keeps its own OS file handle open on `path` for as long as the document object lives, including through a failed open, and on Windows that collided with the upload route's own `TemporaryDirectory` cleanup of that same path immediately after -- a `PermissionError` masking what should have been a clean 422. Opening from memory removes the file handle from the picture entirely. Bounded by the project's existing 25 MiB upload cap, so no new memory concern at this scale.

### Implemented

```
ingestion/parsers/pdf.py   _open_pdf(): opens from bytes, translates
                           fitz.FileDataError / fitz.EmptyFileError /
                           fitz.mupdf.FzErrorBase into ValueError; probe_pdf,
                           _native_blocks and render_pages all route through it
tests/test_pdf_parser.py   test_probe_pdf_raises_value_error_on_a_file_that_is_not_a_pdf
tests/test_process_api.py  test_project_upload_rejects_a_corrupt_pdf_instead_of_500ing
                           (written against the single-file contract, adapted
                           to the batch endpoint's `files` field once that
                           landed)
```

### Verified

- `tests/test_pdf_parser.py`'s new test failed first with the original `fitz.open(path)`, confirming it reproduces the exception family before the fix.
- Reproducing the bug live: rebuilt the `api` container, created a project with one valid PDF and one corrupt one through the New Project dialog, and watched the corrupt file's row spin forever in the files rail with the route logging a raw `pymupdf.mupdf.FzErrorFormat` traceback and returning 500.
- After the fix, rebuilt and restarted the `api` container again and reran the identical case: one `POST .../documents` request, `200`, the good file's row reaches the check icon and the corrupt file's row reaches the alert icon, neither stuck. Console clean.
- Full backend suite: 282 passed. Frontend `tsc -b` and `vite build` clean (unaffected by this change; confirmed only because both were touched moments earlier by the concurrent batch-upload work).

## Batch Document Upload over SSE

Status: complete.

### Discussed and decided

- Keep one `POST /projects/{project_id}/documents` endpoint and accept repeated multipart `files` fields. The synchronous parser, provider, Postgres, and Qdrant clients make a bounded thread pool the smallest concurrency model that provides real overlap without adding Redis, Celery, or another service.
- Own the executor at application scope, not request scope. Production creates one pool from `RuntimeConfig`; `create_query_app` accepts an injected executor for tests and owns shutdown only when it creates the pool itself.
- Reject batch-level failures before the response starts. Ownership, provider setup, file count, total bytes, and Qdrant profile/configuration can therefore retain normal HTTP errors. Once streaming begins, file-specific failures are `file_result` events and do not terminate sibling work.
- Initialize the Qdrant collection before worker fan-out to avoid concurrent collection creation. A wholly invalid batch does not create the collection.
- Correlate results with the zero-based request `index`, not filename. Duplicate filenames are valid and worker completion order is nondeterministic.

### Implemented

```
config.py                  four validated upload settings and env parsing
api/app.py                 shared ThreadPoolExecutor and shutdown lifecycle
api/routes.py              batch validation, per-file worker, SSE results
api/__init__.py            UploadFileResult export
compose.yaml               upload setting defaults forwarded to the API
tests/test_config.py       environment parsing coverage
tests/test_process_api.py  concurrency, isolation, limits, and SSE contract
frontend/src/lib/api.ts    multipart batch client and shared SSE parser
frontend/src/lib/types.ts  file result and batch summary contracts
frontend/src/App.tsx       one request per queue and per-file streamed updates
```

Default limits are 10 files per batch, 25 MiB per file, 100 MiB per batch, and 2 upload workers. All can be overridden through `UPLOAD_MAX_FILES_PER_BATCH`, `UPLOAD_MAX_FILE_BYTES`, `UPLOAD_MAX_BATCH_BYTES`, and `UPLOAD_WORKER_COUNT`.

### Verified

- Focused config, app, and process API integration slice: 36 passed.
- Full backend suite: 282 passed, 94.78% coverage against the 80% gate.
- The concurrency test synchronizes two embedding calls with a barrier and records distinct worker thread IDs.
- The partial-failure test leaves the corrupt file `failed` and the valid sibling `completed` in Postgres.
- Frontend `tsc -b` and Vite production build pass.
- VS Code diagnostics report no errors in changed Python and TypeScript files.
- `docker compose config --quiet` passes; incomplete-marker scan has no matches.
- Running containers were not rebuilt or restarted.

## Delete a Project

Status: complete.

### Discussed and decided

- Requested from a claude.ai screenshot: a three-dot button on each project card opening a menu, with Delete as the item to wire up.
- Scope held to Delete only. The screenshot's menu also has Pin, Edit details, Report and Archive, none of which have any backend support (no pin flag, no rename route, no archived state), and building UI for them would mean either stub buttons or inventing backend features nobody asked for. Same judgment call as the earlier LLM picker: build what's real, preview nothing that isn't.
- No route to delete a project existed before this; `DELETE /threads/{thread_id}` was the closest precedent and this mirrors it: same 204/404 shape, same explicit-child-delete pattern in the store method.
- Checked whether deleting a project needs to cascade to threads or memory before writing the route. `ThreadRow.project_id` and `MemoryRow.project_id` both carry no real foreign-key constraint in the actual migrations (`MemoryRow`'s `ondelete="CASCADE"` on the SQLModel field never made it into a migration, so it does nothing), so a deleted project leaves dangling references in both tables. Traced what that does downstream rather than guessing: the thread route already does `project = owned_project(...)` inside the message-send generator and catches `HTTPException` into an `error` SSE event, so a thread whose project vanished surfaces "Project not found" as a normal in-band error, not a crash. The frontend's `threadProject` lookup already falls back to `null` for the same reason (`projects.find(...) ?? null`). Given the system already tolerates this, cascading deletes into two more tables wasn't needed for correctness, only for tidiness, so it was left alone and written up as a limitation instead of silently "fixed" with an unrequested cascade.
- Qdrant vectors for the project's documents are left in place for a related reason: there is no document-delete route anywhere in this codebase to model a cleanup call after, and building one, plus a payload-filter delete-by-project call, plus tests against a live Qdrant, is a second feature wearing this one's name. The vectors become unreachable (project IDs are UUIDs, never reused) rather than wrong, so nothing is silently broken by leaving them.

### Implemented

```
storage/projects.py        delete_project() added to the ProjectStore protocol
storage/pg_projects.py     PostgresProjectStore.delete_project(): explicit
                           ProjectFileRow delete, then the project row
api/routes.py              DELETE /projects/{project_id}, 204 / 404
lib/api.ts                 deleteProject()
components/Panels.tsx      three-dot button + menu per project card;
                           .project-card split into an outer div and an inner
                           .project-card-hit button so the menu trigger is a
                           sibling, not nested inside the card's own button
App.tsx                    removeProject(): clears activeProjectId/files and
                           returns to the Projects list if the deleted project
                           was the one open
styles/global.css          .project-card-more, .project-card-menu, and the
                           .project-card / .project-card-hit split
```

### Verified

- `tests/test_projects.py`: store-level delete, ownership-scoped, idempotent false on a second delete.
- `tests/test_project_api.py`: route-level 404 for a foreign owner, 204 for the real delete, gone from a subsequent `GET /projects`, 404 on redelete.
- `tests/test_app.py`: the exact-openapi-paths test failed first, as expected, until `/projects/{project_id}` was added to its pinned set.
- 276 backend tests pass, 95% coverage.
- `tsc -b` and `vite build` clean.
- Rebuilt and restarted the `api` container (no bind mount, so the image needed rebuilding to pick up the backend change) and verified live: created a throwaway project, opened its three-dot menu and confirmed no visual collision with the lock icon, deleted it, reloaded the page and confirmed it was gone from a fresh `GET /projects` rather than only removed from local state, and confirmed `curl -X DELETE` on a nonexistent project ID still 404s. Console clean throughout.

### Open

- Threads and project-scoped memory referencing a deleted project are left dangling, tolerated but not cleaned up. Deleting a document from a project, which this would need first to clean up properly, does not exist as a feature yet.
- Qdrant vectors for a deleted project's documents are never removed; they sit unreachable rather than being reclaimed.

## LLM Schema and Prompt Packages

Status: complete.

### Discussed and decided

- Only `Answer` and `AnswerEvidence` move to `rag_ingestion.schemas`; the Pydantic models in `api/routes.py` remain beside the HTTP endpoints they describe.
- Plain and grounded modes each own one complete prompt template. Runtime code supplies `{memory_sections}` and, for grounded mode, `{evidence}` without assembling role and rule fragments in `retrieval/answering.py`.
- Existing imports from `rag_ingestion.retrieval.answering`, `rag_ingestion.retrieval`, and the top-level package remain valid through imports and re-exports.
- Prompt wording, memory precedence and budget, evidence numbering, history ordering, and citation behavior are unchanged.

### Implemented

```
schemas/answering.py       Answer, AnswerEvidence
prompts/answering.py       PLAIN_SYSTEM_PROMPT, GROUNDED_SYSTEM_PROMPT,
                           NO_EVIDENCE_RESPONSE
retrieval/answering.py     runtime memory/evidence rendering and answerers
retrieval/__init__.py      compatibility exports from the schema owner
api/routes.py              direct AnswerEvidence schema import
tests/test_llm_answerer.py package ownership and placeholder contract
```

### Verified

- The new contract test failed first because `rag_ingestion.prompts` did not exist.
- `tests/test_llm_answerer.py`: 20 passed after implementation.
- Full backend suite: 274 passed at 95.26% coverage.
- VS Code diagnostics: no errors in the changed Python files.
- Incomplete implementation marker scan: no matches in changed source or tests.
- Three-layer review found one shadowed test import and one missing top-level export assertion. Both were patched; focused and full suites passed again.

## Memory in the Prompt

Item 9 of `document/rag-workflow/2.multi-turn-memory.md` §17. Status: complete.

### Discussed and decided

- The prompt format was written out and agreed before any code, because ten tests were about to be pinned to it and changing wording afterwards is more expensive than changing it first.
- Section order is the whole point, not decoration. `## Rules` sits after `## User preferences` so it overrides them where the model reads it, and `## Evidence` sits last, next to the question, against the lost-in-the-middle effect.
- Precedence is thread over project over user, per key. The override note names the scope (`this conversation only`) but does not echo the value it replaced. Three options were on the table; this is the middle one. Saying nothing would have violated §6.2, which requires an override to be stated rather than silently dropped.
- `/projects/{id}/answer` gets no memory. It is a stateless one-shot probe with no thread, and keeping personalisation out of it is what makes it usable for inspecting retrieval alone.
- One prompt shape, not two. Keeping the old flat string when memory is absent would have meant two paths that drift, and would have limited the new "do not fake groundedness" rule to users who happen to have memory saved.

### Corrected from the agreed spec

The spec promised that `memory=None` would reproduce the old system string byte for byte. That was not possible: giving `## Rules` its own section, which is what makes the override visible, changes the string whether memory is present or not. One test asserted the old shape via `"evidence" not in content`, a proxy that broke once the plain rules text legitimately mentioned evidence. It now asserts the real intent, that there is no `## Evidence` section and none of the grounded rules.

### Implemented

```
retrieval/answering.py   _ROLE, _ABOUT_USER, _PREFERENCES, _RULES_*, _EVIDENCE
                         _resolve, _memory_line, _memory_sections, _memory_age
                         build_turns(..., memory=None, now=None)
storage/pg_memory.py     recall(): union over the three scopes, one query
storage/memory.py        recall() on the protocol
api/routes.py            load_memory(): honours memory_enabled, returns [] when
                         no store is configured; wired into the thread send
```

### One defect, found by the route test and not by the unit tests

`load_memory` was first written with `list_memories(thread_id=...)`. That filters on the column, and user-scoped rows have a NULL `thread_id`, so every global memory was excluded and only the thread's own reached the prompt. The eleven `build_turns` tests all passed, because they hand memory in directly; only the end-to-end route test failed. Fixed by adding `recall()`, the union query §6.2 specifies, and leaving `list_memories` with its filter semantics for the settings screen.

Worth keeping in mind: unit tests on a pure function cannot see a wrong query feeding it.

### Verified

249 tests, 95% coverage. Both real prompts were printed and compared against the agreed format.

- 11 prompt-shape tests: absent memory, section placement, `pin` excluded, rules after preferences, a preference failing to disable the citation rule, thread over user, project over user, no note without a conflict, plain and grounded rules not mixing, age phrasing, budget overflow reported.
- 5 store tests for `recall`: union rather than filter, another thread's rows excluded, no thread meaning user scope only, deleted and superseded skipped, owner scoping.
- 3 route tests: memory reaches the model, a thread-scoped value wins end to end, `memory_enabled=false` keeps it out.

### Open

- The 1000-character budget drops whole entries in key order once it is reached. It does not prefer preferences over facts, or recent over old. With caps at 10 and 30 per scope this is not reachable in normal use, and the overflow is reported, so no ranking was invented for it.
- `last_used_at` is never written. Nothing marks a memory as used when it lands in a prompt, so the forgetting policy in §6.8 has no signal yet.
- Still open from §17: item 5 (third retrieval source and citation seeding), item 11 (`UnderstandMessage`), item 12 (promotion on a key seen in two threads), item 14 (L2 summary).

## Project Screen, and Home as Plain Chat

Status: complete.

### Discussed and decided

- Requested: the home screen must not let you pick a project, chatting there is plain chat, a project is only reachable through the Projects tab, and opening one shows its own screen where "Chats and tasks" lists that project's chat history and the right rail (Claude's Instructions / Context / Scheduled cards) is replaced by the project's uploaded files.
- Before this, `activeProjectId` did double duty: the project you last clicked and the project your next home message would be sent to. Clicking a card called `newChat()`, so browsing projects silently rearmed the composer. The two meanings are now split: `activeProjectId` is only what the project screen is showing, and a chat's project comes from the open thread's `project_id`.
- So `send(text, projectId)` and `upload(file, project)` take the target as an argument. Reading it from state was what made the old coupling invisible, and with the coupling gone it would have become a real bug: open project A, open a plain chat from Recents, upload, and the file lands in A.
- `GET /threads` has no project filter and returns `project_id` on every row, so the project's chat list filters client-side. No backend change was needed and none was made.
- The list is chats only. The screenshot's "Activity" tab, Instructions and Scheduled cards have nothing behind them on this backend, so they are not drawn.

### Implemented

```
components/Panels.tsx     ProjectPanel: title, composer slot, chats list, files rail
components/Sidebar.tsx    'project' added to the View union
App.tsx                   openProject(), send(text, projectId), upload(file, project),
                          composerFor(project), breadcrumb head, plain-chat home
styles/global.css         .crumbs, .project-layout, .project-rail, .rail-*
```

### Verified

Live in the browser against the running API:

- home shows `Plain chat` in the header, the composer pill and the sidebar footer, with a project already opened in the same session
- project screen renders breadcrumb, `Private · cohere · created Aug 4`, its own composer pill (`Travel policy · cohere`), chats list and files rail
- a message sent from project `aa` created a thread carrying that project; it then appeared under `aa`'s "Chats and tasks" and not under `Travel policy`'s, and opening it from there restored the project pill in the chat header
- the retrieval error path fired on that send, which a plain chat cannot reach, so the `project_id` did travel with the thread
- `New chat` straight after leaving a project screen goes back to plain chat
- the files rail lists `policy.docx` with its `completed` badge; the empty project shows the dashed placeholder
- console clean, `npm run build` clean, probe thread deleted from the backend afterwards

Not verified: a real upload through the rail button, deliberately. It reuses the hidden-input pattern already in the composer and `DocumentsPanel`, and a live upload would have indexed chunks into a real project and spent embedding calls.

### Fixed in passing

`.card-title` and `.card-sub` were inline spans, so every card in Chats, Documents and the new rail rendered title and subtitle on one line with no gap ("pingProject chat · 8/5/2026"). The `margin-top` and `text-overflow` already on those rules only apply to block boxes, so `display: block` is what they were written for.

## Model Picker in the Composer

Status: complete (UI only).

### Discussed and decided

- Requested: the LLM button in the composer should open a box to pick a model, holding Auto, GPT-5.5 and Opus 5, disabled for now.
- Read as a preview of the control, not a working switch. Nothing on the backend takes a model per request: `LLMAnswerer` is constructed once in `api/app.py` from the configured chat provider, and the thread send route has no model field. Wiring the picker would mean a backend change, which was not asked for.
- The pill used to show only the project and embedding provider ("Plain chat", "aa · cohere"). That label was first kept in the `.pill-sub` muted slot behind the model name, then dropped at 15:57 on request: the pill is a model control and the retrieval label belongs to the header and the project screen, both of which still show it. `Composer` no longer takes `providerLabel`.
- The box says "Switching models is not available yet." A menu of three greyed rows with no explanation reads as a bug.

### Implemented

```
components/Composer.tsx    LLMS list, llmOpen state, click-outside + Escape, pill button and popover
components/icons.tsx       CheckIcon for the active row
styles/global.css          .model-select, .model-pill.open, .model-menu and rows
```

### Verified

`npm run build` (tsc -b + vite build) clean. Live in the browser at 5173:

- pill renders as `Auto` and the chevron, nothing else
- clicking opens the box above the pill, right-aligned, in both the centred composer and the docked one inside a thread; not clipped by the scroll container in either
- Auto carries an accent checkmark, all three rows are greyed and unclickable, the note shows
- clicking outside closes it; the `aria-expanded` on the button flips with the state

### Open

Once a send can carry a model, the rows become selectable and `ACTIVE_LLM` in `Composer.tsx` turns into state lifted to `App.tsx`.

## Personalization Tab and Memory Route Tests

Completes the UI half of the memory work: item 10 of `document/rag-workflow/2.multi-turn-memory.md` §17 plus the route tests the storage round left open.

### Discussed and decided

- Requested: a personalization tab in personal settings, and clicking manage memory shows the user's global facts and preferences as cards that can be edited or deleted. Scope for this round was fixed as items 7 + 8 + 10 + UI, with item 9 (reading memory into the prompt) explicitly excluded.
- So the tab must say out loud that memory does not affect answers yet. A settings screen that silently stores preferences nothing reads is worse than no screen: the user changes a value, sees it saved, and concludes the model is ignoring them.
- The About-you fields map onto closed-list keys (`nickname`, `occupation`, `response_language`, `response_format`) rather than a free-text blob. A blob cannot supersede, and supersede is the thing that stops two contradictory values coexisting.
- Save is a plain POST, not a PATCH. The backend supersedes on an existing key in the same scope, so one call is both create and overwrite, and the client does not need to know which case it is in.
- Custom keys are offered, but only through the `other:<slug>` branch with client-side validation matching the server regex. The alternative, letting the user type any key, is exactly the drift the closed list exists to prevent.
- Memory loads in its own effect rather than joining the bootstrap `Promise.all`. The routes answer 503 without a memory store; folding them into the bootstrap would take projects and threads down with them.

### Implemented

```
tests/test_memory_api.py            18 tests over the 9 memory and settings routes
components/Personalization.tsx      PersonalizationTab + ManageMemoryDialog
components/Panels.tsx               SettingsPanel split into Account / Personalization
lib/api.ts                          list/create/update/delete memory, get/patch settings
lib/types.ts                        Memory, UserSettings, MEMORY_KEYS
App.tsx                             memory state, save/edit/delete/toggle handlers
styles/global.css                   memory cards, switch, settings headings
```

### Verified

Backend: 229 tests pass. The first assertion in `test_memory_api.py` pins `GET /memory/proposals` to the literal route rather than `/memory/{memory_id}`; FastAPI matches in declaration order, so reordering the routes now fails a test instead of silently returning 404 on a valid proposals call.

Frontend, live in the browser against the rebuilt API:

- created entries from the About-you form and from the dialog
- added `other:favourite-editor` with the value `Neovim, không dùng VS Code`, which round-tripped intact
- edited a card in place
- deleted a card, and confirmed in Postgres it was marked `deleted_at` rather than removed
- re-saved `nickname`, and confirmed the previous row carried both `superseded_by` and `deleted_at` while the new row stayed live
- read the audit trail back as ADD / UPDATE / DELETE with per-memory sequence
- toggled `memory_enabled` off, reloaded the page, and saw the switch and all field values restored from the backend
- console clean, `tsc -b && vite build` clean

Probe rows were removed from `memory`, `memory_audit` and `user_settings` afterwards.

### Files

- `tests/test_memory_api.py`
- `frontend/src/components/Personalization.tsx`
- `frontend/src/components/Panels.tsx`
- `frontend/src/lib/{api,types}.ts`
- `frontend/src/App.tsx`
- `frontend/src/styles/global.css`
- `CHANGELOG.md`

### Status

Complete for the agreed scope. Memory is fully manageable through the UI and persists correctly.

**Open:** item 9. Nothing in the answer path reads these rows, so the tab's stored values do not change replies. The notice in the tab has to come out at the same time as that lands.

### Two defects found in the second UI pass

- **A rejected write cleared the inputs as if it had succeeded.** `saveMemory` and `editMemory` caught the error, raised a toast, and resolved, so the dialog's `.then()` fired on the failure path as well. Filling the 30-entry cap and adding one more returned 409 and silently ate the typed value. Both now return a boolean and the dialog clears, or closes the editor, only on success. An error path that resolves like a success is worse than one that throws, because the caller has no way to tell the difference.
- **Error toasts were painted over by the modal backdrop.** `.toast` at `z-index: 20`, `.modal-backdrop` at `40`. The cap message was rendering with the correct text the whole time and was simply not visible, which is why the first pass read as "no feedback" rather than "wrong feedback". Raised to 60. Pre-existing, and it applied equally to the New project dialog's upload failures.

### Known limitations

- Only user-scoped memory is shown. Thread- and project-scoped rows exist in the store and have no UI, so a thread-scoped memory can only be seen through the API.
- No confirmation on delete. The row is a soft delete and recoverable from the database, but not from the interface.
- The switch writes `user_settings.memory_enabled` and nothing reads it yet, for the same reason as item 9.
- The list does not refresh while open. Memory written by another client, or seeded outside the UI, only appears after the next fetch.

## Memory Storage and the Candidate Budget

Implements items 1 and 7 to 10 of `document/rag-workflow/2.multi-turn-memory.md` §17.

### Discussed and decided

- Three scopes, not two. `thread` is the default because global memory is injected into every future conversation, so anything merely situational (a client name pasted into a chat) must not reach it. That is a privacy boundary, not a quality one.
- Supersede keys on (scope, target, key). A thread-scoped preference overrides the user-scoped one for its own thread only, so replacing it would destroy the thing being overridden.
- Nothing is hard deleted. `deleted_at` is marked and the audit trail kept, because the only defence left against a bad write is the owner being able to review it. That is also why `GET /memory` and `DELETE /memory/{id}` are required rather than convenient.
- Caps are per scope, and `thread` gets 10 against 30. A thread accumulating more than ten instructions is far more likely to be an over-eager extractor than a user issuing ten instructions.
- The closed key list is English in code and Vietnamese in the design document. Free-form keys drift ("occupation" one turn, "job" the next), which breaks both supersede and the promotion count, so anything outside the list goes to `other:<slug>` where supersede is weaker by design.
- Reading memory into the prompt is deliberately not wired. The store landing and the answer path consuming it are separate steps, and mixing them would have made the store untestable on its own.

### Implemented

```
db/tables.py            memory, memory_proposal, memory_audit, user_settings
storage/memory.py       contracts: 3 scopes, 3 types, 3 sources, closed key list, caps
storage/pg_memory.py    PostgresMemoryStore
api/routes.py           GET/POST /memory, PATCH/DELETE /memory/{id},
                        GET /memory/proposals, POST .../confirm, DELETE ...,
                        GET/PATCH /settings
retrieval/chain.py      _fill_pool: per-source candidate budget
migrations/             cb5a1dde9f49 memory audit sequence
```

### Two defects found and fixed

- **The audit trail had no ordering key.** Ordered by `created_at` with a random `audit_id` as tie-break. This host's clock resolution puts an ADD and the UPDATE after it on one identical timestamp, so the order was arbitrary and the two tests asserting `["ADD", "UPDATE"]` and `["ADD", "DELETE"]` were passing by luck rather than by construction. Added `memory_audit.sequence`, allocated per memory inside the write. Identical defect and identical fix to `threads.sequence`, which the Postgres migration had already hit.
- **The candidate pool was cut by insertion order.** `list(candidates.values())[:max_candidates]` drops whichever source merged last, which is the global fallback. Harmless today because two sources at k=30 sum to exactly the 60 cap, but the design adds a third source, and at 3 x 20 the fallback would have been emptied while `global_k >= 1` still claimed to guarantee it. No existing test covered this; the guard test only asserted that `global_k = 0` is rejected at construction.

  The first attempt at the fix, plain round-robin over the raw source lists, still failed: the fallback spent its share on leaves the scoped branch had already provided, because both lists share a head. Its real contribution sits past that head. So each source is now reduced to what earlier sources did not return, and the budget is shared over those remainders.

### Files

- `backend/rag_ingestion/db/tables.py`
- `backend/rag_ingestion/storage/{memory,pg_memory,__init__}.py`
- `backend/rag_ingestion/api/routes.py`
- `backend/rag_ingestion/retrieval/chain.py`
- `backend/migrations/versions/cb5a1dde9f49_memory_audit_sequence.py`
- `tests/{test_memory_store,test_app,test_retrieval_chain}.py`
- `CHANGELOG.md`

### Status

Storage and routes complete. Verified on 2026-08-05: 229 tests at 95% coverage (`pg_memory.py` 98%), `alembic upgrade head` applied to the running Postgres, `alembic check` clean, and a `downgrade -1` / `upgrade head` round trip. The new pool test was confirmed red on the old slicing before the fix.

**Open**, from §17 of the design: item 5 (third retrieval source and citation seeding), items 9 and 10 (reading memory into the prompt, with system rules ranked above `preference`), item 11 (`UnderstandMessage`, the one LLM node that rewrites the query and extracts memory), item 12 (promotion on a key seen in two threads), item 14 (L2 thread summary).

### Known limitations

- `memory_audit` has no foreign key to `memory`, deliberately, so the trail outlives its subject. But `memory` itself cascades from `threads`, so deleting a thread still removes thread-scoped rows while their audit entries remain, pointing at a `memory_id` that no longer resolves.
- Caps are enforced by a count inside the write transaction, not by a constraint. Two concurrent writers on the same scope can both pass the check and land one row over the cap.
- Nothing reads these rows yet, so none of the read-path decisions in the design (scope precedence, override reporting, the injection cap) are exercised by a test.

## SQLite to Postgres

### Discussed and decided

- Follow the EngCoach stack (`sqlmodel` + `psycopg2-binary` + `alembic`, engine module with a
  tuned pool, `db/base.py` importing every table module for autogenerate) so both projects share
  one mental model.
- Keep the frozen Pydantic contracts as the store boundary. `PostgresProjectStore` and
  `PostgresThreadStore` satisfy the existing protocols and map rows to those contracts, so routes,
  response models, and their assertions did not change. ORM rows never leave the store modules.
- Tests run against a real Postgres, one schema per test. The alternative (SQLite for tests) would
  stop testing what actually ships, and dialect differences are precisely what bites later.
- Existing data dropped. It was 1 empty project and 4 test threads.
- Qdrant is untouched. Vectors stay there; `pgvector` is not needed unless Qdrant is replaced.

### What EngCoach did and did not provide

Copied: the ORM choice, the engine/pool settings, the `db/base.py` autogenerate pattern, the
`env.py`-reads-settings approach, and the compose service shape (healthcheck plus
`depends_on: condition: service_healthy`).

**Not available:** a testing pattern. EngCoach has 3 test files and no DB-backed tests at all; its
`conftest.py` only fixes `sys.path`. The per-test schema harness here had no prior art to copy and
was the largest single risk to the existing 175 tests.

### Four things Postgres forced that SQLite had hidden

1. **Message order had to become data.** SQLite ordered messages by implicit `rowid`. Postgres has
   no equivalent, so `thread_messages.position` is now an explicit column with a uniqueness
   constraint per thread.
2. **Timestamps needed `timestamptz`.** The stores work in tz-aware UTC and the API serialises the
   offset. A plain `timestamp` column accepts the value and silently discards the zone, which would
   have changed every timestamp the API returns.
3. **Cascade delete needed to be explicit.** The FK carries `ON DELETE CASCADE`, but deleting
   through the ORM does not emit it unless the children are loaded. `delete_thread` now issues the
   child delete itself. Verified by counting rows: zero orphans.
4. **Counter allocation needed a row lock.** `sequence` and `position` are `MAX(...) + 1` reads.
   Under SQLite's single-writer model that was safe by construction; on Postgres two concurrent
   writers could read the same maximum, so both are taken under `SELECT ... FOR UPDATE`.

### Alembic

- `env.py` reads the URL from `RuntimeConfig`, so migrations and the app share one `.env` and one
  precedence rule. `alembic.ini`'s `sqlalchemy.url` is deliberately left blank so a stale value
  there can never quietly win.
- `script.py.mako` imports `sqlmodel`. Autogenerate emits `sqlmodel.sql.sqltypes.AutoString` but
  does not add the import, so the first generated migration failed with `NameError` until the
  template was fixed. Fixing the template means future migrations are correct too.
- Initial migration verified two ways: `alembic check` reports no drift against the models, and
  `downgrade base` then `upgrade head` round-trips cleanly.
- The container runs `alembic upgrade head` before uvicorn. `create_runtime_app(create_tables=True)`
  remains for local runs without Alembic; after a migration it is a no-op.

### Files

- `backend/rag_ingestion/db/{__init__,base,session,tables}.py` (new)
- `backend/rag_ingestion/storage/pg_projects.py`, `pg_threads.py` (new)
- `backend/rag_ingestion/storage/{projects,threads}.py` (reduced to contracts and protocols)
- `backend/migrations/` and `backend/alembic.ini` (new)
- `backend/rag_ingestion/{config,api/app}.py`, package `__init__.py` re-exports
- `tests/conftest.py` (new), all store and API test modules ported
- `compose.yaml`, `Dockerfile`, `pyproject.toml`

### How to run

```
docker compose up -d db          # Postgres on localhost:5432
docker compose up -d --build     # api migrates then serves
pytest                           # needs the db service running
cd backend && alembic revision --autogenerate -m "..."   # after changing tables.py
```

### Status

Complete and verified on 2026-08-04. 175 tests pass against a real Postgres 16 with a per-test
schema. The container runs the migration on start. A live two-turn conversation recalled a name
across turns, and the rows plus `timestamptz` values were confirmed directly in the database.
Cascade delete verified by row count.

### Known limitations

- `create_tables=True` stays the default in `create_runtime_app` for local convenience. In any
  environment where Alembic runs, Alembic is the authority; the call is a no-op there.
- `PROJECT_DATABASE_PATH` is gone. Anything still setting it is silently ignored; use `DATABASE_URL`.
- Tests need the `db` compose service running. `conftest.py` fails with one clear instruction
  rather than erroring in every test.

## Centralised Config and a Free Chat Model

### Discussed and decided

- One `config.py` is the only place a runtime setting is read. Everything else takes a
  `RuntimeConfig`. `os.environ` now appears in exactly one line of the codebase.
- Process environment wins over `.env`. Compose injects real variables and must keep working;
  a stale file must never shadow an injected value.
- No `pydantic-settings` dependency. The `.env` format in use is plain `KEY=value`, and a
  20-line parser covers it. Every dependency is permanent code we do not control.
- `.env` is searched at two fixed locations (working directory, then project root) rather than
  walked upward. An unbounded walk can silently pick up an unrelated `.env` from a parent
  directory, which is a confusing failure to debug.
- Default chat model is `openrouter/free`, so chat works with no credits. Paid Anthropic slugs
  stay available through `OPENROUTER_MODEL`.

### Implemented

- `config.py`: `parse_env_file`, `find_env_file`, `resolve_environment`, and `RuntimeConfig`
  (moved from `api/app.py`, which re-exports it so existing imports keep working).
- Parser handles comments, blank lines, an `export ` prefix, matched surrounding quotes, and
  whitespace, while keeping `=` and `#` inside values. No interpolation, no shell semantics.
- `ENV_FILE` points at a specific file and skips the search.
- `DEFAULT_OPENROUTER_MODEL = "openrouter/free"`, mirrored in the Compose default.

### Defect found while writing the tests

`resolve_environment` originally treated `env_file=None` as "search for one", with no way to say
"read no file at all". Isolated unit tests therefore loaded the project's real `.env` and asserted
against live credentials, which both broke the tests and printed a real API key into test output.
Added an explicit `read_env_file` switch; `None` remains "search", `False` is now "use none".

### Files

- `backend/rag_ingestion/config.py` (new)
- `backend/rag_ingestion/api/app.py` (config block removed, 59 lines), `backend/rag_ingestion/__init__.py`
- `tests/test_config.py` (new, 21 tests), `tests/test_app.py`
- `compose.yaml`, `frontend/README.md`, `CHANGELOG.md`

### Status

Complete and verified live on 2026-08-04. 174 backend tests pass. `config.py` reads `.env` from
disk with no variables injected (all four provider keys resolve). The rebuilt container serves
`openrouter/free`, and a two-turn conversation confirmed history replay end to end:

```
[user]      My name is Dat. Answer briefly.
[assistant] Hello, Dat. How can I help you today?
[user]      What is my name?
[assistant] Your name is Dat.
```

A streamed answer was also confirmed rendering in the browser.

### Still open

`.env` defines `DATALAB_API`, but the code reads `DATALAB_API_KEY`, so `config.datalab_api_key`
resolves to `None` and no `ScanParser` is wired. Scanned PDFs and images are still rejected.
Renaming that one key in `.env` fixes it.

## Conversation Threads and OpenRouter Chat

### Discussed and decided

- One `POST /threads/{id}/messages` endpoint serves both modes. `project_id` lives on the thread, not the message, and is immutable: if it could change mid-conversation the history becomes incoherent and citations from earlier turns point at a project that is no longer attached.
- Streaming is SSE. Chosen up front rather than retrofitted, because moving from a JSON response to SSE later would change the endpoint contract and the frontend send path together.
- `MockAnswerer` stays and is selectable through `USE_MOCK_ANSWERER`. The earlier decision to defer generation existed so retrieval stays observable without token spend; adding a real answerer must not delete that capability.
- History is capped (40 messages / 24k characters). Without a cap, every turn resends the whole thread and spend grows quadratically.
- Titles are derived client-side and sent on create. The server defaults to "New chat" and never calls a model to name a thread.
- Retrieval hits are streamed for display but not persisted; only the citation IDs are stored. Hits are large and rebuildable, and this is a harness, not an archive.
- The frontend is a harness for exercising the API. No frontend tests; the old suite was removed rather than maintained against the rewritten data layer.

### Implemented

```
backend/rag_ingestion/
  chat.py                  ChatTurn + ChatCompleter protocol (leaf module, no internal deps)
  storage/threads.py       threads + thread_messages, cascade delete, monotonic sequence
  providers/openrouter.py  OpenRouterChat: streaming + non-streaming chat completions
  retrieval/answering.py   LLMAnswerer + shared build_turns() beside MockAnswerer
  api/routes.py            6 thread routes, SSE on messages, history window
  api/app.py               OPENROUTER_API_KEY / OPENROUTER_MODEL / USE_MOCK_ANSWERER
```

- Owner scoping on every thread operation; a foreign user gets 404 on read, rename, delete, and message send.
- The user turn is persisted before generation begins, so a provider failure leaves the question saved and surfaces as an SSE `error` event. HTTP status is already 200 by then, which is inherent to SSE.
- `chat.py` sits at the package root because putting the contract under `providers/` closed an import cycle: `providers/registry.py` already imports `retrieval/reranking.py`, and `retrieval/__init__.py` re-exports `answering`.
- Frontend: `streamThreadMessage()` reads the SSE stream via `fetch` + `ReadableStream`, buffering on `\n\n` boundaries; conversations load from the server; plain chat needs no project.

### Two defects found and fixed during the work

- **Recents ordering was non-deterministic.** `datetime.now(UTC)` on this Windows box has ~15ms resolution, so creating two threads and posting a message all landed on one identical `updated_at`, leaving the order to arbitrary tie-breaking. Replaced with a monotonic `sequence` column assigned inside the write transaction; `updated_at` stays for display. Renaming deliberately does not bump it, so a rename does not jump a thread to the top.
- **`Answerer` was not `@runtime_checkable`**, unlike `ProjectStore` and `ThreadStore`, so the protocol could not be contract-tested the way the other stores are. Added the decorator.

### Files

- `backend/rag_ingestion/{chat,storage/threads,providers/openrouter}.py` (new)
- `backend/rag_ingestion/{api/routes,api/app,retrieval/answering}.py`
- `backend/rag_ingestion/{storage,retrieval,providers}/__init__.py` re-exports
- `tests/{test_threads,test_openrouter,test_llm_answerer,test_thread_api}.py` (new), `tests/test_app.py`
- `compose.yaml`, `frontend/src/`, `frontend/package.json`, `frontend/vite.config.ts`
- `CHANGELOG.md`

### Known limitations

- Both stores open their own connection to the same SQLite file. Writes rely on the default 5s busy timeout; adequate for a single-worker dev API, not for concurrent writers. WAL was deliberately not enabled because it is a database-level setting that would silently change the project store's behavior too.
- Retrieval hits vanish on reload; stored citations remain.
- SSE cannot report a mid-stream failure as an HTTP status, so clients must handle the `error` event.

### Status

Backend complete and verified on 2026-08-04: 153 tests pass, all six routes live in the rebuilt container, the OpenRouter adapter streams correctly against the real API (validated with a free model), and a live browser round trip created a thread, persisted it, survived a reload, and replayed its stored message.

**Blocked:** generation on the configured model. OpenRouter returns `402 Insufficient credits, this account never purchased credits`. The default slug `anthropic/claude-opus-5` was confirmed to exist in OpenRouter's model list, so this is a billing state, not a configuration error. Add credits, or set `OPENROUTER_MODEL` to one of the `:free` slugs to exercise generation at no cost.

## Package Layout by Pipeline Stage

### Discussed and decided

- The flat 19-file module had become hard to read, so files are grouped by the pipeline stage they belong to rather than by type.
- `models.py` stays at the package root. Every stage imports it, so pushing it into a package would only add an arbitrary hop.
- Each package re-exports its public names from `__init__.py`, which keeps `rag_ingestion.chunking`, `rag_ingestion.retrieval`, and `rag_ingestion.providers` valid as import paths and leaves the top-level `rag_ingestion` exports untouched.
- Two modules were renamed while moving so the module name is not a duplicate of its package: `retrieval.py` to `retrieval/chain.py`, `query_api.py` to `api/routes.py`.
- Pure move, no behavior change. The test suite is the check: same 95 tests, same 93% coverage.

### Implemented

```
backend/rag_ingestion/
  __init__.py, models.py
  ingestion/    router, pipeline, reconciliation, parsers/{datalab,image,office,pdf}
  chunking/     chunker, structure, references
  indexing/     qdrant_index, local_embedder
  retrieval/    chain, automerge, reranking, answering
  providers/    registry, adapters
  storage/      media, projects
  api/          app, routes
```

- Internal imports rewritten across 32 modules and the test suite.
- Dockerfile entrypoint moved to `rag_ingestion.api.app:app`.
- Encoding repair: a PowerShell `Get-Content -Raw` / `Set-Content` round trip during the import rewrite decoded three test files through the ANSI codepage and mangled their Vietnamese literals. Reversing it needed a hybrid cp1252 encoder, because cp1252 leaves 0x81, 0x8D, 0x8F, 0x90, and 0x9D undefined and those bytes had arrived as bare U+00xx code points, so a plain `encode("cp1252")` raised and repaired nothing. BOMs added to 38 files were stripped.
- Lesson recorded: do file rewrites through the editing tools or Python, never through PowerShell 5.1 text cmdlets, on any file containing non-ASCII.

### Files

- `backend/rag_ingestion/` (all modules moved, 7 new package `__init__.py`)
- `tests/` (imports updated; `test_references.py`, `test_retrieval_chain.py`, `test_structure.py` encoding repaired)
- `Dockerfile`
- `CHANGELOG.md`

### Status

Complete. Verified with 95 tests at 93% coverage on 2026-08-04, no stubs or TODOs, and no remaining references to the old module paths.

## Query Chain, Structure-First Chunking, and Media References

### Discussed and decided

- Defer real LLM generation; the mock answerer returns the retrieved chunks so retrieval work stays observable without API spend.
- Defer URL, audio, and video adapters. Route images through the same Datalab OCR path as scanned PDF pages.
- Skip building an evaluation harness for now and rely on published results when choosing between techniques.
- Keep cross-cutting work (lazy re-parse, schema metadata extraction, auto lexicon, near-duplicate dedup, abstention) for later.
- `media_ref` is a key plus a swappable `MediaStore` interface, not an S3 dependency; the filesystem backend is enough until production.
- Auto-Merge promotes parents for context, but evidence must always resolve back to citable leaves so a citation never points vaguely at a parent.
- A soft route must never make the correct leaf unreachable, so the global candidate fallback is always on and `global_k=0` is rejected at construction.

### Implemented

- `structure.py`: natural-unit detection from the document itself, with a four-part coverage test and Vietnamese/English clause patterns; clause boundaries populate `section_path` when no heading structure exists.
- `references.py`: anchor index, citation extraction, `refers_to` relation linking, glossary extraction, and defined-term lookup, all deterministic regex with no model calls.
- `automerge.py`: the three HiChunk merge conditions with multi-level climbing, only-child shortcut, and document-order leaf resolution.
- `retrieval.py`: Q0 soft routing, Q1 scoped plus global union, rerank, Auto-Merge, capped one-hop reference expansion, capped definition pinning, and Q6 leaf resolution, with per-stage diagnostics.
- `media.py`: `MediaStore` protocol, `LocalMediaStore` with root-escape rejection, and deterministic media keys.
- `parsers/image.py` and image routing for PNG, JPEG, TIFF, WEBP, and BMP; Datalab content type now derived from the suffix.
- Scanned PDF page rendering to PNG at 150 DPI, with `media_ref` flowing from block to chunk to query payload.
- Chunk contract extended with `child_ids`, `defines`, and `media_refs`; the routing hierarchy is now leaf to section to document with real parent links.
- Query response extended with `evidence_leaf_ids`, `routed_doc_ids`, and `diagnostics`; hits carry `level` and `media_refs`.

### Files

- `backend/rag_ingestion/{automerge,media,references,retrieval,structure}.py`
- `backend/rag_ingestion/parsers/image.py`
- `backend/rag_ingestion/{answering,app,chunking,pipeline,qdrant_index,query_api,router,__init__}.py`
- `backend/rag_ingestion/parsers/{datalab,pdf}.py`
- `tests/{test_automerge,test_image_parser,test_media,test_references,test_retrieval_chain,test_structure}.py`
- `CHANGELOG.md`

### Status

Complete. Verified with 95 tests at 93% coverage on 2026-08-03, with no stubs, TODOs, or unimplemented paths.

## Backend Rename and Claude-Style Chat Frontend

### Discussed and decided

- Rename `src/` to `backend/` and add a sibling `frontend/` so the repository shows both halves of the product at the top level.
- Keep the `rag_ingestion` package name and internal layout untouched; only the package root moves.
- Build the chat UI as a visual match for claude.ai dark mode, driven entirely by the existing API contracts.
- Reach the API through a Vite dev proxy on `/api` instead of adding CORS middleware, so the FastAPI app stays unchanged.
- Map every sidebar entry to a real feature rather than shipping decorative dead buttons; drop claude.ai chrome with no backend equivalent (Artifacts, Products, Starred).
- Treat the pinned embedding provider as the "model" shown in the composer pill.

### Implemented

- `backend/` package root with updated setuptools `packages.find`, pytest `pythonpath`, and Dockerfile COPY.
- Typed API client for `/health`, `/projects`, `/projects/{id}/documents`, and `/projects/{id}/answer` with `X-User-ID` injection, FastAPI validation-detail flattening, and an offline-backend error path.
- Claude dark theme in plain CSS: serif wordmark and greeting, 272px sidebar, rounded composer, starter chips, and orange accent, with no external fonts or CSS frameworks.
- Greeting screen, conversation view with user bubbles, assistant answers, a thinking indicator, and collapsible per-hit source evidence showing breadcrumb and rerank score.
- Projects panel with pinned-profile display and provider-aware creation, blocked with guidance when the backend reports no configured provider.
- Documents panel and composer upload for PDF, DOCX, XLSX, and PPTX, reporting indexed chunk counts and per-file processing status.
- Chats panel with search and delete, plus localStorage conversation history keyed to the browser.
- Customize panel for switching the `X-User-ID` identity, which reloads projects for the new owner.

### Files

- `backend/rag_ingestion/` (moved from `src/rag_ingestion/`)
- `pyproject.toml`, `Dockerfile`, `.dockerignore`, `.gitignore`
- `frontend/src/`, `frontend/tests/`, `frontend/vite.config.ts`, `frontend/package.json`
- `CHANGELOG.md`

### How to run

```
docker compose up -d          # API on http://localhost:8000
cd frontend && npm install
npm run dev                   # UI on http://localhost:5173
```

### Status

Complete. Verified on 2026-08-03 with 56 backend tests, 73 frontend tests at 96.67% coverage, clean `tsc -b` and `vite build`, a Docker image build plus in-image route listing, and a live browser session where `/api/health` and `/api/projects` returned 200 through the proxy.

Not verified end to end: project creation, document upload, and answering need a provider API key. The running API reports `configured_providers: []`, so those paths are covered by mocked tests only.

## Private Projects and Provider Retrieval

### Discussed and decided

- Keep projects private to one user for the MVP and defer tenant/team sharing.
- Pin one immutable embedding profile per project while storing vectors in one shared collection per provider profile.
- Authorize ownership in SQLite before Qdrant access, then duplicate owner and project filters in every vector query.
- Use Cohere, Voyage, or Jina for embedding and reranking; keep answer generation deterministic and mocked.

### Implemented

- SQLite project and multi-file metadata store with owner-scoped access, pinned profiles, processing states, content hashes, canonical document IDs, and schema migration.
- Owner/project identity propagation through parsing, document IDs, chunking, Qdrant payloads, keyword indexes, and citable search filters.
- HTTPX adapters for Cohere, Voyage, and Jina native embedding/reranking contracts with sanitized provider errors and mocked transport tests.
- Project create/list, document upload/list, reranked query, and mock answer routes using `X-User-ID`.
- Environment-backed provider bundles, profile collection naming, optional credentials, and persistent Docker project metadata.

### Files

- `src/rag_ingestion/`
- `tests/`
- `compose.yaml`
- `document/provider-embedding-reranking-answer.md`
- `CHANGELOG.md`

### Status

Complete. Verified with 56 tests, 92.74% coverage, zero editor diagnostics, valid Compose configuration, and live Docker health/ReDoc/OpenAPI smoke tests on 2026-08-02.

## Document Core

### Discussed and decided

- Normalize supported files into one `Document` plus atomic `Block[]` contract.
- Parse Office files natively and never route them through OCR.
- Classify PDF pages independently, keeping native pages local and sending only scan pages to Datalab.
- Keep source blocks and locators atomic during cross-page reconciliation.
- Keep embedding and reranking providers outside the parser package.

### Implemented

- Strict Pydantic schemas for documents, blocks, relations, and tagged locators.
- Deterministic document and block IDs based on source identity and content.
- Extension router for PDF, DOCX, XLSX, and PPTX.
- Native OOXML parsing for DOCX hierarchy/tables and PPTX slides/speaker notes.
- Native XLSX parsing with formulas, cached values, merged ranges, rows, and cells.
- PyMuPDF page probes and native text extraction.
- Datalab `/api/v1/convert` multipart submission and result polling behind `ScanParser`.
- Reconciliation for repeated headers, cross-page continuations, and figure-caption relations.
- Unified `process_document` API.

### Files

- `pyproject.toml`
- `src/rag_ingestion/`
- `tests/`
- `CHANGELOG.md`

### Status

Complete. Verified with 19 tests and zero editor diagnostics on 2026-08-02.

## Chunking and Hybrid Index

### Discussed and decided

- Chunk reconciled `Document + Block[]` data by structure before token size.
- Keep leaf chunks citable and use section/document chunks only for routing.
- Preserve source text, block IDs, spans, locators, confidence, relations, and breadcrumbs in the index contract.
- Keep embedding providers behind an `Embedder` protocol and lock each Qdrant collection to one embedding profile.
- Store named dense and sparse vectors together, then fuse independent candidates with RRF.

### Implemented

- Deterministic leaf, section, and document chunk identities and parent links.
- Prose grouping, sentence-boundary splitting without overlap, minimum-tail merging, and special-block bypass behavior.
- Table row indexing without duplicate table-container or child-cell points.
- `EmbeddingProfile` and provider-neutral `Embedder` contracts.
- Qdrant collection creation with named cosine dense vectors and IDF sparse vectors.
- Language-aware sparse tokenization that preserves identifiers and adds CJK character/bigram tokens.
- Deterministic Qdrant point IDs, full provenance payloads, profile compatibility checks, and tenant-filtered RRF search.

### Files

- `pyproject.toml`
- `src/rag_ingestion/chunking.py`
- `src/rag_ingestion/qdrant_index.py`
- `src/rag_ingestion/__init__.py`
- `tests/test_chunking.py`
- `tests/test_qdrant_index.py`
- `CHANGELOG.md`

### Status

Complete. Verified with 27 tests, 92.96% coverage, and zero editor diagnostics on 2026-08-02.

## FastAPI Query Service

### Discussed and decided

- Build the HTTP layer as an app factory that receives `QdrantHybridIndex` and `Embedder`, keeping provider configuration outside FastAPI.
- Read tenant identity from `X-Tenant-ID` instead of accepting it in the request body.
- Return only citable leaf evidence and expose citation provenance without internal tenant or embedding metadata.

### Implemented

- `GET /health` readiness check for the configured Qdrant collection.
- `POST /query` with strict query, language, and result-limit validation.
- Tenant and citable hard filters in Qdrant hybrid search.
- Ranked response contracts containing source text, block IDs, spans, locators, confidence, hierarchy, references, breadcrumb, and score.
- HTTP 422 responses for missing or blank tenant headers and invalid request values.
- HTTP 503 response for unavailable collections or incompatible index configuration.
- FastAPI and Uvicorn runtime dependencies.

### Files

- `pyproject.toml`
- `src/rag_ingestion/query_api.py`
- `src/rag_ingestion/qdrant_index.py`
- `src/rag_ingestion/__init__.py`
- `tests/test_query_api.py`
- `CHANGELOG.md`

### Status

Complete. Verified with 32 tests, 93.14% coverage, and zero editor diagnostics on 2026-08-02.

## Docker API Runtime

### Discussed and decided

- Keep FastAPI as the public query boundary and Qdrant as an internal search backend.
- Run FastAPI in its own container while reusing the existing Qdrant container on the host.
- Supply a deterministic local embedder for development, while retaining the provider-neutral `Embedder` contract.

### Implemented

- Environment-backed `rag_ingestion.app:app` Uvicorn entrypoint.
- Deterministic normalized feature-hash embeddings with an immutable local embedding profile.
- Docker image based on Python 3.12 slim.
- Compose service publishing FastAPI on port 8000 and connecting to Qdrant through `host.docker.internal:6333`.
- Docker build exclusions for virtual environments, tests, backups, caches, and Qdrant storage.
- Runtime tests for embedding determinism, OpenAPI/ReDoc availability, and read-only collection behavior.

### Files

- `Dockerfile`
- `.dockerignore`
- `compose.yaml`
- `src/rag_ingestion/app.py`
- `src/rag_ingestion/local_embedder.py`
- `tests/test_app.py`
- `CHANGELOG.md`

### Status

Complete. Live ReDoc returned HTTP 200 at `http://localhost:8000/redoc`. Verified with 34 tests, 93.46% coverage, and zero editor diagnostics on 2026-08-02.

## Document Process API

### Discussed and decided

- Accept supported source documents through the FastAPI boundary rather than exposing Qdrant directly.
- Execute the existing parse, reconcile, chunk, embed, and index pipeline synchronously for local development.
- Take tenant identity from `X-Tenant-ID` and keep uploaded files temporary.

### Implemented

- Multipart `POST /documents/process` for text-layer PDF, DOCX, XLSX, and PPTX; scanned PDF requires a configured `ScanParser`.
- Safe basename normalization and extension validation before writing upload data.
- Streaming file writes with a configurable 25 MiB default limit and automatic temporary-directory cleanup.
- Parse and chunk counts plus document identity in the HTTP 201 response.
- Qdrant upsert of citable leaves and routing hierarchy using the configured embedder profile.
- HTTP 413, 415, 422, and 503 mappings for upload, format, parsing, and index failures.
- ReDoc/OpenAPI publication of `/documents/process`.

### Files

- `pyproject.toml`
- `src/rag_ingestion/query_api.py`
- `src/rag_ingestion/__init__.py`
- `tests/test_process_api.py`
- `tests/test_app.py`
- `CHANGELOG.md`

### Status

Complete. Verified with 37 tests, 93.38% coverage, zero editor diagnostics, and a live Docker upload/query smoke test on 2026-08-02.
## Project Delete Purges Vectors and Media

Status: complete.

- Came out of `document/rag-review-2026-08-11.md` finding #2 (delete_project only removed Postgres rows; vectors orphaned forever in the shared collection). User asked two questions first: does the Qdrant payload carry `project_id`, and can deletion key off it. Verified yes on both — payload carries `owner_user_id` + `project_id` (`qdrant_index.py:387-388`) and both have KEYWORD payload indexes.
- Per the user's directive, capability was tested before implementation: a scratchpad script ran filter-based `client.delete(FilterSelector(...))` against the in-memory client AND the live server on localhost:6333 with a 3-tenant fixture (same owner/other project, other owner/same project id). Both passed: exactly the target tenant's points removed, re-delete idempotent, no collection created. Script deleted after use.
- Implementation (TDD, RED confirmed before GREEN): `QdrantHybridIndex.delete_by_project` reusing `_tenant_conditions`; `MediaStore.delete_prefix` + `LocalMediaStore` rmtree implementation (media keys are `{doc_id}/...` so one prefix removes page images + markdown + anchors); `DELETE /projects/{id}` route now orders cleanup vectors → media (best-effort) → cache invalidate → Postgres row last, so any failure leaves the row for a retry.
- Known tradeoff, consistent with the search path: the route resolves the provider bundle via `project_bundle`, so a provider that is unconfigured or profile-mismatched returns 503 and blocks deletion until realigned — same invariant the query path already enforces.
- Test-env note: this Mac runs a local Homebrew Postgres on 127.0.0.1:5432 that shadows the docker `rag-postgres` for loopback connections ("role postgres does not exist"). Suite must run with `TEST_DATABASE_URL`/`DATABASE_URL` pointed at the host LAN IP (e.g. `192.168.1.9:5432`) to reach the docker DB. 589 tests pass.
- Files: `backend/rag_ingestion/indexing/qdrant_index.py`, `backend/rag_ingestion/storage/media.py`, `backend/rag_ingestion/api/routes.py`, plus tests `test_qdrant_index.py`, `test_media.py`, `test_project_api.py`.

## Document Lifecycle: Trash/Restore, Dedup, Per-Question Selection

Status: complete (backend + frontend). Builds on "Project Delete Purges Vectors and Media".

- Design settled over three rounds with the user: (1) doc delete is project-scoped — confirmed `doc_id` embeds owner+project so filter `owner+project+doc_id` is right; (2) user proposed NotebookLM-style checkboxes for "disable temporarily" — adopted, replacing the planned persistent DISABLED state column, so exclusion is a per-message request parameter; (3) user set the trash policy: deleted files stay restorable by sha256 for a TTL, past it re-upload re-indexes. Implemented the TTL against `trashed_at` in Postgres rather than a Redis key — same behavior, no dependency on optional Redis, one source of truth (deviation noted to the user, accepted direction "hướng bền hơn").
- Flow: DELETE = set `trashed_at` (vectors/media kept, retrieval excludes via must_not built from Postgres per query). Upload computes sha256 (already existed) and now reads it: active match → `duplicate`, trashed match within `UPLOAD_TRASH_TTL_SECONDS` (default 7d) → `restored` (zero provider calls), expired → lazy purge + fresh index. `?permanent=true` purges (delete_by_doc + delete_prefix + row) with a last-reference guard for legacy duplicate rows sharing one doc_id.
- `disabled_doc_ids` rides `QueryRequest`/thread messages into all Q0/Q1/reference-expansion searches and the retrieval cache key (prefix bumped v3). Frontend: checkbox per indexed document, unchecked list sent per message, reset on project switch.
- Deliberate choices: no auto-purge scheduler (no background-job infra; expired trash purges lazily on matching upload or explicitly), markdown/media stay readable for trashed docs so old citations keep working, `done` SSE gained `duplicates`/`restored` counters.
- Note: a second Claude session was editing the frontend concurrently (answer versions / citation chips, 23:25 entry); changes did not collide — verified by `tsc -b` + `vite build` after both.
- 597 backend tests pass. Files listed in the changelog entry [2026-08-11 23:35].

## Auto-Merge Context Reaches the Prompt (finding #4)

Status: complete.

- Root cause established in discussion first: two consumers were collapsed into one list. Citations need citable leaves (Q6's output), generation needs the merged contiguous text (`context_chunks`) — the answering path was built around the leaf list before auto-merge landed, and nobody wired the new field in. Worse than "shuffled": `descendant_leaf_ids` only sees recalled leaves, so promotion added zero new text; unrecalled siblings and connective prose never reached the model.
- Design confirmed with the user before coding: HiChunk order stays (rerank leaves → merge after; parents never re-ranked — cross-encoder windows are short and merge depends on budget), parent position = best recalled leaf's rank via max, not mean (relevance is "contains the answer", a mean dilutes a bullseye leaf).
- Change: `context_evidence()` in routes feeds the prompt from `context_chunks` (leaf fallback for stale cached outcomes); hits/sources/citations stay leaf-level; `/answer` citations switched from the answerer's evidence ids (would be parent ids) to the response's leaf hits. `chain.py` sorts `selected` by best-descendant rank.
- RED first: thread test asserting one contiguous `[1]` block + no `[2]` + leaf citations; `/answer` test updated to `Retrieved 1 evidence chunks`. 599 tests pass.

## Sparse Leg: BM25 TF + Thai Char-Grams (finding #6)

Status: complete. Auth (#1) explicitly pended by the user; #5 dev fixes (LOG_LEVEL default, Postgres port/password) proposed and awaiting approval.

- Clarified for the user first: Qdrant hybrid = BYO-sparse. The server stores client-sent values and contributes only IDF (`Modifier.IDF`); TF shape is entirely client-side. Elasticsearch-style built-in BM25 does not exist in Qdrant — hence the raw-count shortcut had gone unnoticed.
- Change is write-path + read-path via one shared function: `_sparse_vector(text, language, intent=...)` — documents get BM25 TF (k1=1.2, b=0.75, avgdl=256 fixed because the chunker pins leaf size; matches FastEmbed's constant-avgdl approach, no corpus statistic to track), queries keep raw counts (saturation is a document-side concept).
- Thai: `_GRAM_PATTERN` (renamed from `_CJK_PATTERN`) gains `฀-๿`; unsegmented Thai runs now produce char unigrams+bigrams instead of one giant hash token. Proper word segmentation (pythainlp) noted as a possible later upgrade — new dependency, needs explicit approval.
- Profile version default bumped v1→v2 → new collections; old dev projects pinned to v1 will 503 ("profile mismatch") and must be recreated + re-uploaded. This is the immutable-profile rule doing its job, told to the user.
- 4 RED-first tests: saturation asymptote, length penalty, query raw counts, Thai doc/query gram overlap. 603 tests pass.

## Thai Segmentation (nlpo3) + Script-Based Language Labeling

Status: complete.

- Two user decisions drove the shape: nlpo3 over pythainlp (lighter, same newmm algorithm, same maintainers), and the answer to "should I add document language detection?" — no: doc-level statistical detection cannot guarantee tokenizer routing (mixed documents, short blocks), the codepoint scan already guarantees it; what's worth adding is deterministic per-block script labeling.
- Discovery during implementation: nlpo3 ships no dictionary. Vendored PyThaiNLP's 62k-entry newmm wordlist into `rag_ingestion/data/words_th.txt` (Apache-2.0); `[tool.setuptools.package-data]` carries it into the wheel `pip install .` builds in the Docker image. Also hit the `indexing → ingestion/__init__ → storage → indexing` cycle when language.py lived under ingestion/ — moved to package root.
- Segmentation is additive: Thai runs produce dictionary words ON TOP of char bigrams (compound dictionary entries like "ภาษีมูลค่าเพิ่ม" stay whole, so a "ภาษี" query still needs the gram overlap; out-of-dictionary terms too).
- `Block.language` now labeled at `process_document` (majority script; th/ko/ja/zh/vi/en; None when <4 script letters). Downstream consumers to build later: language filters, per-script sentence splitting in the chunker (still open, with whitespace token counting).
- 613 tests pass. Still inside the v2 profile window — no v2 collection exists yet, so one rebuild + re-upload covers both this and the BM25 change.

## Retrieval Hot-Path Perf (findings #17/#18)

Status: complete. MMR dropped by user decision; Q0-section-identity and nested-hierarchy deferred conditionally (revisit when eval on real data shows section-level misses, or when real documents have deep TOCs).

- Investigated with the user before coding; two corrections surfaced during the discussion and are worth remembering: the docker deployment already runs the Redis embedding cache (compose sets REDIS_URL; startup log `embedding_cache=on`), so repeated query embedding was 2 Redis GETs, not 3 provider calls — the real waste was Qdrant round-trips. And the user spotted that the doc-scoped Q1 pass is exactly the global search plus a filter, hence fully redundant whenever routing covers every project document.
- Fixes: ensure_collection memoized + removed from hybrid_search (reads never create; route guards existence); embed-once in run_retrieval with dense vectors passed to all searches; scoped-pass skip behind `project_doc_ids` (computed from the rows the trash exclusion already fetches — no extra Postgres query). Rerank depth (max_candidates=60) deliberately left alone: within industry band, one Cohere billing unit either way, and cutting it blind trades recall for an unmeasured saving — revisit after MTRAG eval on real data.
- 6 RED-first tests; suite 619 passed. No reindex needed.

## Deferred Understanding for Plain Chat + Smalltalk Gate (finding #19)

Status: complete for plain chat; project-mode parallelization consciously rejected.

- The user's two asks: stop understand from blocking plain-chat TTFT, and stop paying for it on messages that obviously carry nothing ("nếu luôn viết lại cho chat thường thì quá phí").
- Key insight that makes the deferral safe: plain chat consumes nothing from understand in the current turn — the rewrite has no retrieval to feed and extracted memory is only read by later turns (this turn's recall happens in `load_memory` beforehand). So the call moved to the post-`done` slot alongside `refresh_summary`, best-effort, same inputs.
- `worth_understanding` gate: ALWAYS-run pattern (first-person/naming/instruction markers, vi+en) overrides everything; exact-phrase smalltalk set skips; pure emoji/punctuation skips; default runs. All gate errors err toward running. Deviation from design §3.1 "no exceptions" recorded here and in the changelog at the user's direction.
- Project-mode parallel understand||retrieval was proposed, then killed by arithmetic before implementation: the join waits on understand either way (LLM 1–3s vs Qdrant ~0.3s), so the overlap saves ~0.3s at the price of an executor and a rewrite-dependent cache key. Left serial. Revisit only if understand moves to a fast local model.
- 624 tests pass.

## Deep Agent Spike (deepagents 0.7.5)

Status: spike complete — verdict GO, with caveats. No production code changed; deps live in the optional `agent` extra only.

- Context: the user's target is a comprehensive chatbot (tools, skills, later subagents), not RAG-plus-memory. Decision: adopt deepagents as the generation engine, keep the FastAPI shell + Q0-Q6 retrieval pipeline + Postgres memory store as tools. The user's three security criteria became the spike's acceptance tests.
- Results (`backend/scripts/spike_deepagent.py`, rerunnable):
  1. Tool loop with tenant-scoped tools: PASS (fake model, no network).
  2. Isolation: PASS with zero configuration — the default filesystem backend is StateBackend (virtual, per-conversation). `read_file("/etc/passwd")` → "not found", `ls /` → empty. No folder-per-project or VM needed; VM only becomes relevant if code execution is ever enabled.
  3. Live tool-calling through OpenRouter `openrouter/free`: PASS (24.6s on the free tier; the model searched then answered from the tool result). Free-tier latency is high and flaky — one later rerun exceeded 5 minutes; a real deployment should pin a tool-capable paid model. The model did NOT spontaneously save "My name is Dat" as memory on the free model — memory-tool reliability needs prompt/model-tier work in the real integration.
- Default agent surface: ls/read_file/write_file/edit_file/delete/glob/grep (virtual FS) + task (subagents) — trim via harness options in the real integration if not wanted. GenericFakeChatModel needs a bind_tools override to test agents offline.
- Open items for the integration phase (not spike-blockers): SSE adapter with per-tool whitelist labels (no raw tool args to the client), OpenRouter sticky-key/prompt-cache behavior under the LangChain client, forcing first-turn retrieval for project chat vs model-decides, understand shrinking to rewrite-only, gate removal.

## Memory Tools in the Generation Call (llm_with_tools)

Status: complete. Deepagents paused by user decision ("tránh sử dụng quá phức tạp không cần thiết") — the optional `agent` extra and spike script remain for later.

- The user's final architecture call after the deepagents spike: keep the whole existing flow (project chat always searches through the pipeline), change only the generation call — plain LLM → llm_with_tools with two memory tools. This is spike-validated pattern #1 from the memory research (ChatGPT bio / Letta): the reply model's own judgment is the gate.
- Implementation: native tool loop in OpenRouterChat.stream (no framework), ToolSpec/ToolHandler in the chat protocol, per-request tenant-scoped tools in ApiContext.memory_tools mapping local→project|thread and global→user. write_or_propose reused so caps/proposals/supersede/audit hold; PROJECT scope added to it.
- Consequences swept in the same change: regex smalltalk gate deleted (user: not scalable multi-language), understand shrunk to rewrite-only + project-only (plain chat = 1 LLM call/turn), thread→user promotion retired with the extraction path. write_understanding + memory_routing kept but orphaned — flagged for cleanup.
- Coordination note: the API layer was concurrently split into routers/ by another session mid-implementation; scope was announced via cross-session message and the work landed on the new structure without conflicts.
- Caveat carried from the spike: whether the free-tier model actually calls the save tools reliably is a model-quality question — verify live after the container rebuild, and consider a paid tool-capable model if saves are missed.
- 626 tests pass.

## Generation on create_agent; Tools as a Domain Module

Status: complete. Supersedes the hand-rolled tool loop from the previous entry.

- Two user corrections drove this: tools do not belong in `api/` (definition is domain logic, the API layer only binds tenancy), and the loop should be LangChain's `create_agent` rather than something we maintain. The earlier hand-rolled loop came from reading "pause deepagents" as "avoid the LangChain stack entirely" — it was the narrower `create_agent` that was wanted.
- Shape: `tools/memory.py` builds LangChain `@tool`s against a `MemoryWriter` protocol; `deps._ContextMemoryWriter` supplies tenant + `write_or_propose`; `providers/langchain_chat.py` runs `create_agent` behind the unchanged `ChatCompleter` protocol, so routes, SSE contract and route tests were untouched.
- Kept deliberately outside LangChain: sticky key rotation (model built per call), usage accounting (usage_metadata → accounting keys), error sanitization. Only the `model` node's text streams to the user.
- Live smoke found what unit tests could not: the closed key list vs the model's free-form keys. Fixed with `normalize_key` (alias map + `other:<slug>` fallback) instead of rejecting, because a rejection surfaces after the answer streamed and is invisible to the user. Verified end to end against the real Postgres store.
- Known variance: on OpenRouter's free router, tool-calling reliability changes run to run. Mechanism verified; if production saves are missed, pin a tool-capable paid model rather than adding heuristics.
- 641 tests pass.

## Thread Summary as a Code-Owned Item List (delta folds, no more full rewrites)

**Discussed (2026-08-15):** `document/rag-workflow/3.lesson.md` surveyed ten 2025-2026 agent-memory papers against the shipping backend. Strongest finding: `LLMSummarizer` re-generated the entire summary each fold under a 150-word ceiling with an explicit "compress the oldest material first" rule — the context-collapse failure ACE (ICLR 2026) names and measures. User confirmed this as the first fix, chose the delta design over LangChain structured output (merge is code either way; JSON-in-text + per-op validation matches `understanding._parse`), and asked for the code in a `rag_ingestion/summary/` package with schemas under `schemas/`.

**Implemented:**
- `backend/rag_ingestion/schemas/summary.py` — `SummaryItem` (strict, frozen, `s<N>` id), `SummaryOp` (model output, extra keys ignored, shape-validated per kind), `SummaryOpKind`, `SummaryDelta` (documentation contract; parsing is per-op).
- `backend/rag_ingestion/summary/items.py` — `parse_items`/`render_items`, stored form `[sN] text` per line in the existing text column; legacy prose parses as one `s1` item, so no migration and old threads stay addressable.
- `backend/rag_ingestion/summary/merge.py` — `parse_delta` (code-fence strip, per-op skip, op cap) and `apply_ops`: deterministic merge, UPDATE moves the item to the back, eviction from the front (recency-of-touch, never oldest-id-first), unknown ids skipped and logged, evicted ids returned for logging.
- `backend/rag_ingestion/summary/summarizer.py` — same `Summarizer` protocol and failure contract as before (any failure keeps the previous summary), so `deps.refresh_summary`, `app.py` wiring, and every fake summarizer in tests kept working with only import-path changes.
- `backend/rag_ingestion/prompts/summarization.py` rewritten: model returns `{"ops": [...]}`; unmentioned items are kept; DROP only on contradiction, never for length. The old rule 5 (150 words, compress oldest first) is gone.
- `backend/rag_ingestion/config.py`: `DEFAULT_SUMMARY_MAX_ITEMS = 16`, `DEFAULT_SUMMARY_ITEM_MAX_CHARS = 160`, `DEFAULT_SUMMARY_MAX_OPS = 20`.
- Old `rag_ingestion/summarization.py` → `archive/pre-trash-20260815/`.

**Status: complete.** 34 summary tests green (`test_summary_items.py`, `test_summary_merge.py`, rewritten `test_summarization.py` — headline test: a fact the model never mentions survives the next fold). Thread/window/app suites 106 passed. Known cost: rendered summary ceiling grows from ~400 tokens to ~640 (16×160 chars); tune `max_items` down if it bites. Remaining lesson-doc items (2-7: `last_used_at` writes, `forget_memory` tool, overflow order, memory_routing decision, eval, abstention rule) not started.

## Memory Package + Promotion Restored (threshold 5)

Status: complete. Remaining memory gaps triaged with the user: project/thread memory UI and proposal UI are pending; `last_used_at` and `pin` stay open.

- `rag_ingestion/memory/` now holds models, store, routing and tools; `storage/__init__.py` no longer re-exports memory names, so there is exactly one import path per contract.
- Promotion regression found during the review the user asked for: moving extraction into the generation tools silently dropped design 6.5's thread→user promotion, and the dead code kept its green unit tests, which is exactly why it went unnoticed. Restored inside `_ContextMemoryWriter.save` (the single write path now), THREAD scope only, threshold 5 per the user.
- Deleted `write_understanding`/`_write_understanding` rather than leaving them: code that looks live but is unreachable misleads the next reader and hides regressions like the one above.

## LRU Eviction, last_used_at, and Removing Pin

Status: complete. Remaining from the memory triage: LLM consolidation for the global scope (user chose LRU for thread/project, LLM for user scope, running in the post-`done` slot with an eviction counter) and the project/thread memory UI, both still pending.

- Decision recorded: caps are budgets, not walls. The proposal path stays only for a scope full of `user_explicit`/`user_confirmed` rows — what the user typed is not the pipeline's to discard.
- "Used" defined as reaching the model, not being recalled: `select_memories()` is the shared, pure selection (scope resolution + character budget) that both the renderer and `record_memory_use` run. This deliberately avoids changing `build_turns`' signature, and it means the LLM is never asked to report which memories it used — the answer is computable on our side.
- Eviction placed inside `_claim_slot` for atomicity (advisory lock already held, live rows already loaded). Sort key: never-used first, then oldest `last_used_at`, `created_at` as tiebreak.
- `pin` deleted after confirming zero rows in Postgres. Document pinning stays a future feature on `ProjectFileRow` + a retrieval filter.
- 694 tests pass.

## Memory Carry Budget: Items, and Fair Ordering

Status: complete.

- Found by the user's question, not by a test: with a character budget and alphabetical ordering, the tail of the key space could never reach a prompt. Combined with the LRU eviction added the same day, that made it a deletion pipeline for alphabetically-late keys — the eviction change turned a display quirk into data loss.
- Budget is now `MEMORY_MAX_ITEMS` (25) with `MEMORY_CHAR_BUDGET` (2000) as a backstop. Whole lines only; no truncation, then or now.
- Carry order: scope specificity → never-shown-yet → most recently used → newest. The never-shown-first rule is what breaks the starvation loop; a memory shown once acquires a timestamp and joins the normal rotation.
- Render order stays alphabetical on purpose: selection changes which lines appear, never their order, so the cacheable system prefix only changes when the memory set really changes.
- Open tension, accepted: when the scopes together hold more than the carry budget, something is always left out. The real fix is not to let scopes stay over-full — that is LRU eviction (done) and the global-scope LLM consolidation (pending).

Follow-up the same day, on the user's suggestion: the "never used" bucket was collapsed into a single `last_relevant_at = last_used_at or created_at` timeline, shared by prompt selection and eviction. It fixed a real bug in the eviction rule (a memory saved one second ago outranked-for-deletion one last read a year earlier) and made the carried set stable enough for prompt caching. Accepted consequence: a memory that never reaches the prompt can never improve its rank and will eventually be evicted — defensible because the criterion is age-of-relevance rather than the arbitrary alphabetical one it replaced, and the real answer to a permanently over-full scope is the pending LLM consolidation.

## Memory Review (2026-08-15)

Status: review complete; two bugs fixed, three findings open for a decision.

- Cleared by the adversarial pass: advisory-lock scope, tenant filtering on every query, no promotion loop.
- Fixed: Protocol/implementation drift on `add_memory(source_message_id)`; raw value comparison in the tool write path (now uses `routing.normalize`, matching what the retired extraction path always did).
- Open decisions for the user: (1) `route`/`scope_for`/`MemoryAction` are orphaned again — delete or keep for the pending LLM consolidation; (2) a promoted user memory is shadowed by the thread copy that triggered it, so it is never touched in those threads and drifts toward eviction — touch displaced rows too, or accept; (3) promotion does not observe whether its write degraded into a proposal.
- Current flow, for the record: tools write (scope fixed by tool identity, key normalized, dedup against the turn's recall, cap → LRU evict → proposal only when the scope is all user-written, THREAD writes check promotion at 5 distinct threads); recall unions three scopes; `select_memories` resolves scope conflicts and carries 25 items ranked by scope specificity then `last_relevant_at`; after `done`, the carried set is touched and the summary refreshed.

## Model-Aware Context Budget: Catalog, Measured Gauge, Pre-flight Folding, Manual Compact

**Discussed (2026-08-15):** Continuing from `document/rag-workflow/3.lesson.md`. The old summary trigger counted messages (`total - 7 >= 8`), which passed once and then folded on every turn; the window was a fixed 6 messages regardless of model; generation was capped at 2048 tokens for every model. User decisions: pre-flight only (post-answer folding still blocked the composer via the SSE stream, so it bought nothing); measure prompt size from the provider's own `prompt_tokens` and calibrate chars/token per thread per model; server computes the gauge percent; picker can change model any time (applies from the next message); gauge is click-to-compact from 50%, warns at 80/90%; summary item cap fixed at 32; no Redis; prompt caching deferred (OpenAI auto-caches, Anthropic needs explicit breakpoints — flagged).

**Implemented (all five stages, backend 728 tests green, frontend tsc + build clean):**
- Stage 1 — `data/models.json` (6 models, verified OpenRouter slugs; GPT-5.6 pinned to 272k usable to stay out of OpenAI's overcharge zone) + `schemas/model_catalog.py` + `model_catalog.py` (`load_catalog`, `resolve` with default fallback, `usable_tokens`, `context_percent`); migration `d4b8f1c3a7e2` adds `last_prompt_tokens/chars/model_id` to `threads`; `record_llm_usage` writes them; `GET /models`; `ThreadView.context_used_percent` computed server-side on every thread response.
- Stage 2+3 — `ChatCompleter` protocol + both providers accept per-call `model`; `plan_history` (floor / message cap / char budget, opener always kept, returns fold boundary), `preflight_turns` (calibrated estimate, fold before generation, 0.7 refill amortization), `fold_through` (watermark-incremental, bounded per call) replace `refresh_summary`/`history_turns`/`window`; `max_tokens` per model; `done` carries `context_used_percent` + `model_id`; dead knobs deleted from config.
- Stage 4 — `POST /threads/{id}/compact`: chained bounded folds down to the floor window, in-process per-thread lock (409), 503 without summarizer.
- Stage 5 — frontend: real model picker (Auto + catalog, per-message `model_id`, restored from `last_model_id`), `ContextGauge` (hidden → info → click-to-compact ≥50% → amber 80 → red 90), percent from thread open / done / compact.

**Status: complete.** Deferred, tracked here: prompt caching breakpoints for Anthropic models (5a), effort wiring into the generation payload (5d — field exists in the catalog, nullable), per-message `model_id` audit column, lesson-doc items #2-#7 (last_used_at writes, forget_memory tool, overflow order, memory_routing decision, eval, abstention).

## BaseAgent and Typed Memory Tools

Status: complete.

- `rag_ingestion/core/agent.py` holds `BaseAgent` (create_agent-backed). Generation, understanding and summarization all construct one; `OpenRouterChat` is archived. Path deviates from the user's literal `backend/core/agent.py` because setuptools packages everything under `backend/` — a top-level `core` package in site-packages is a name collision waiting to happen.
- Memory tools take a `SaveMemoryArgs` pydantic model, so the JSON schema carries enums instead of a description asking nicely. `MemorySlot` = closed key list + `other`; `custom_key` supplies the name in the `other` case only. Live-verified that the failure this replaces (model answering `name`/`profession`) no longer occurs.
- Next candidate on the same track: `LLMUnderstander` still parses JSON out of free text. With `create_agent` underneath, a `response_format` would make that structured, removing the hand-written parser.

## core/llm.py, and Three Memory Decisions

Status: complete.

- `BaseAgent` (create_agent) now takes its model from `core/llm.build_chat_model`, which wraps `init_chat_model`. Swapping providers is `LLM_PROVIDER=anthropic` plus a base_url, not an edit. Clients cached per distinct configuration; `DEFAULT_LLM_PROVIDER` kept in `config.py` to avoid an import cycle through `core`.
- Decision (2/3b): stop writing `last_used_at`. It could not carry information while selection is deterministic from the same rank — the first ordering locked itself in — and it recorded "shown", not "used". Column and `last_relevant_at` kept so a later survey can switch it back on; `touch`/`record_memory_use` deleted rather than left as unreachable code.
- Decision (3a): scope routing (`route`/`scope_for`/`MemoryAction`) deleted. Tool identity decides scope.
- Decision (3c): caps kept, sized by scope lifetime — thread 10, project 30, user 50 (up from 30). 500 was tried first and rejected as too loose. A full scope evicts its oldest, so the cap decides what is forgotten rather than what can be written.
- Still open: the global-scope LLM consolidation, and the survey into what a real "this memory mattered" signal would look like.

## Summary Quality: Duplicate Guard, Two-Phase Fold, Usage Counters

**Discussed (2026-08-16):** After reviewing the fold against ACE (ICLR 2026) and "How Memory Management Impacts LLM Agents" (ACL 2025), three gaps were identified: no usefulness signal (LRU punishes facts that are right the first time), no dedup, and a single LLM call doing both extraction and curation. User decisions across the design rounds: (a) drop embedding-based dedup entirely — embeddings are not smarter than the LLM and merge "deadline for A" with "deadline for B"; rely on a stronger `SUMMARY_MODEL` plus one mechanical exact-match guard; (b) adopt the two-phase fold, accepting that the user waits longer on fold turns — background/pipelined extraction rejected as too expensive; (c) counters come from the model via a trailing `<!--used:...-->` marker rather than from server-side token overlap, since a paraphrase carries a fact without sharing a token; (d) buffer everything after `<!--` until confirmed, and swallow it whole if the turn is stopped mid-marker; (e) eviction candidates are items with `folds >= 1` (user's rule, replacing an arbitrary "first 8" window), lowest `used/(folds+1)` first.

**Implemented:**
- `summary/merge.py` — exact-match ADD guard (normalize, fold into a touch); `parse_facts` for the extract phase; ageing pass (`folds += 1` for items that existed before the fold); `_weakest` eviction ranking with the tie-breaks above; UPDATE carries counters over.
- `summary/summarizer.py` — `summarize` is now extract → curate, each with its own prompt and its own failure path; no facts means no second call.
- `prompts/summarization.py` — split into `EXTRACT_*` and `CURATE_*`; curate is explicitly told to look for an existing item first "even when the wording is completely different", and that an invented id loses the fact it carried.
- `summary/items.py` — `[sN|u:N|f:N] text` storage format (backward compatible), plus `render_for_prompt` which strips counters for anything a model sees.
- `summary/marker.py` — `UsedMarkerFilter`, a chunk-fed state machine over the answer stream.
- `prompts/answering.py` — `USED_MARKER_INSTRUCTION`, appended only when the summary section exists.
- `api/deps.py` — `record_summary_use`: validates ids against the stored summary, credits `used`, leaves the watermark alone.
- `api/routers/threads.py` — filter wired into the stream loop; crediting runs after `done` inside try/except.

**Status: complete.** 725 backend tests pass. Deferred and still tracked: eval on the four MemoryAgentBench competencies (the thing that would let any of this be called an improvement rather than a mechanism), prompt-caching breakpoints for Anthropic models, effort wiring, lesson-doc items #2-#7.

## Share Chat by Link

**Discussed (2026-08-16):** User asked for a `Share` button on the right of main-head and asked what it would take. Decisions taken in the conversation: (1) **snapshot** semantics, not a live view; (2) **login required to create** a link; (3) confirmed that **logged-out visitors can read** a link — creating and reading are separate surfaces. User then asked for an evaluation of the plan before implementation. That evaluation surfaced items the first plan had missed, all of which are in the build: title not being snapshotted (renaming leaked the new name), the login rule existing only in the UI, no `noindex`, silent truncation past `list_messages`' cap, the OpenAPI route-inventory test, clipboard failure on non-secure origins, the Share button's behaviour for anonymous users, and the public page having to mount without the app shell.

One assumption in the plan was verified rather than trusted: "messages are append-only, so a watermark equals a snapshot". `compact_thread` was read end to end — it advances `summarized_through_position` and writes `summary`, and never touches `thread_messages`. Rows are only deleted when the whole thread is deleted, which the FK cascade turns into a dead link, which is the wanted behaviour.

**Implemented (backend):**
- `thread_shares` table: `share_id` (`shr_<32 hex>`, both PK and URL token), `thread_id` (FK, ON DELETE CASCADE), `owner_user_id`, snapshot `title`, `shared_through_position`, timestamps. Unique on `thread_id`, so one live link per chat; revoking deletes the row, so re-sharing mints a different token and the old link stays dead.
- Share methods live on `ThreadStore`/`PostgresThreadStore` rather than a new store, so no new wiring in `routes.py`/`app.py`. `get_share` is deliberately owner-free — it backs the public route, where the token is the only credential.
- `POST /threads/{id}/share` (create-or-return-existing, `{"refresh": true}` extends the snapshot and keeps the token), `GET`, `DELETE`. Anonymous callers get 403 `login_required_to_share` before the ownership check, so the answer is "log in", not "not found".
- `api/routers/share.py`: public `GET /share/{token}`, `SHARE_MAX_MESSAGES = 500`, `PUBLIC_PREFIX` shared with the middleware exemption so the two cannot drift.
- Migration `a1c9e5b7d2f4`, guarded with `has_table` like the users registry.

**Implemented (frontend):**
- `main.tsx` branches on `/share/<token>` *before* `Root`, so the public page never mounts the app shell. Verified in the browser: only `/api/share/...` is requested and localStorage stays empty.
- `ShareDialog` — create / copy / update / revoke, link shown in a selectable read-only field so a blocked clipboard on an http origin costs nothing, "N messages shared · M newer not included" when the snapshot has fallen behind.
- `SharePage` — header with a "Try EslothAI" CTA, transcript through the existing `Message` component (new `readOnly` prop hides the vote buttons), 404 and loading states, mobile breakpoint at 640px.

**Verified:** 15 new integration tests against real Postgres (creation, anonymous 403, non-owner 404, idempotence, refresh, revoke, re-share, thread deletion, snapshot isolation, rename isolation, payload scrubbing, no-identity read with the fallback off, noindex on 200 and 404, truncation). Full suite 740 passed. Frontend typechecks and builds. Live: public page, invalid-link state, mobile 390px, Share button placement, logged-out click opening the login modal, and the dialog's shared state.

**Status: complete.** Deferred: Open Graph tags for link unfurls, and per-IP rate limiting on the public route before this is exposed publicly.

## Message Overflow Menu: Fork and Report

**Discussed (2026-08-16):** User asked for a `...` button on the right of the message actions opening two items — "Continue in a new chat" (fork icon) and "Report message" (flag icon) — plus a Report dialog matching a Pi.ai screenshot: a "What type of content are you reporting?" dropdown with the placeholder "Select an option", an optional details textarea with a 1000-character counter, the line "Submitting this feedback will send the entire conversation to EslothAI to improve.", and Cancel/Submit.

The request described UI only, but neither item can be UI-only without the interface lying: `POST /threads/{id}/messages` always calls the LLM, so there is no way to continue a copied conversation without a new endpoint, and a Submit that discards the report makes the dialog's sentence false. Presented that, and the user chose to build both for real.

Two things decided without asking, both stated back for override: the fork cuts at the clicked message inclusive (branching from the middle leaves the later turns behind, which is the point), and the dropdown offers Harmful or unsafe / False information / Not helpful / Something else.

**Implemented (backend):**
- `POST /threads/{id}/fork` with `through_position` (`ge=1`; a position past the end copies what is there rather than 422-ing on a race with a turn that landed meanwhile). Copies into a new thread with a fresh id, same project, `"<title> (continued)"` truncated to the 200-char column. New message ids, positions renumbered from 1, original `created_at` kept, summary and watermark deliberately not inherited.
- Anonymous callers refused with 403 `login_required_to_fork` — `count_user_messages` spans every thread an identity owns, so copied messages would eat the 3-message trial.
- `message_reports` table and `POST /reports`. `thread_id` is not a foreign key and the conversation is stored inline as JSON, captured server-side, so the record outlives the reported chat. Reason is a `Literal` on both sides; details capped at 1000 to match the counter. Open to anonymous identities by design.
- `fork_thread` needed an explicit `session.flush()` after the thread row: the message rows carry a foreign key to a row that does not exist database-side until then, and the flush order was inserting them first.

**Implemented (frontend):**
- `MessageMenu` inside `Message.tsx`, rendered only for saved assistant messages — both actions need the stored position, so an error bubble or a stopped stream shows no menu. Closes on outside click and Escape, flips up when within 130px of the viewport bottom.
- `ChatMessage` and `ThreadMessage` gained `position`, threaded through `openThread` and both stream handlers.
- `ReportDialog` matching the reference; Submit disabled until a type is picked.
- `forkFrom` prepends the new thread to the sidebar and opens it; a 403 opens the login modal.

**Verified:** 14 new integration tests (anon 403, copy contents/positions/roles, distinct message ids, original untouched, project carried, non-owner 404, position past end, position 0 rejected, no inherited summary; report stored, survives thread deletion with its snapshot, details optional, bad reason 422, over-long details 422, non-owner 404, anonymous allowed). Full suite 754 passed. Frontend typechecks and builds. Live in the browser: menu placement and labels, Report dialog against the reference screenshot, a real submit landing in Postgres with the conversation snapshot, the toast, and fork-while-logged-out opening the login modal. The fork happy path in the UI was exercised with the network call stubbed, because a real Google login cannot be driven from this harness; the server side of it is covered by the tests above.

**Status: complete.**

## Memory Research (4.lesson.md)

Status: complete, and it changes two priorities.

- Written around the five open questions in `4.memory.md` §8, not around a paper list. Evidence labels throughout; a section dedicated to numbers that must not be quoted.
- Raised: **consolidation** is now the top memory item, because it is the only forgetting mechanism with any support in the literature. Lowered: nothing to do about decay or LLM-scored importance — both are unsupported.
- New concrete plan for `last_used_at`: extend the existing `<!--used:-->` marker to memory ids, log only, and revisit once there is data. RMM validates both the signal (F1 90.2) and the danger of using it naively (Recall@5 54.3 → 34.2 when fed straight into retriever training).
- Recorded honestly: our caps + LRU eviction appears in no surveyed system. It is defensible (the alternative default, unbounded growth, is equally unevidenced and has a measured cost) but it is convention, and the doc says so rather than dressing it up.

## Share by Email: Invite-Only Access and Continue-in-Your-Own-Copy

**Discussed (2026-08-16):** User asked to add email invitations to sharing: only the person with that address can open it, they can chat, and it is saved under "Shared with you".

Three things were decided without asking (stated back for override): match the address server-side from the token and require `email_verified`; invite shares require the reader to log in, since there is no address without a token; and shared *chats* get their own sidebar section rather than the Projects screen's "Shared with you" tab, which is about projects.

Two questions were blocking because they are data-access boundaries, and the user chose:
- **Where the guest chats: in a copy of their own** (fork-on-write), not in the owner's thread. No shared mutable state, no cross-user leakage in either direction.
- **Project documents: no retrieval for the guest.** Not one byte of the owner's library goes out.

Those two answers combine into something better than either check: the guest's copy is created with `project_id = NULL`, so retrieval has nothing to run against. The rule is enforced by the shape of the data rather than by a flag in the chat path, and it also means the snapshot semantics of a share survive intact (the guest never appends to the owner's thread).

**Implemented (backend):**
- `thread_shares.access` (`link` | `invite`, defaulting to `link` so no existing link changed meaning) and `thread_share_recipients` (share_id, normalized email, unique per pair, indexed on email).
- `add_share_recipient` flips access to `invite` as part of naming somebody, so "only that address can open it" is true the moment the owner says so. `remove_share_recipient` deliberately leaves access alone: emptying the list must not silently republish the chat.
- `POST/DELETE /threads/{id}/share/recipients`, `access` on `POST /threads/{id}/share`, `GET /shared`, `POST /share/{token}/fork`.
- `VerifiedIdentity.email_verified`; a second ContextVar carries the address, set only when verified. `normalize_email` lives in `storage/threads.py` and is used by both the writer and the matcher.
- The auth middleware's `/share/` handling changed from "skip entirely" to "optional": absent token passes, present token is verified.

**Implemented (frontend):** invite field and recipient list in the share dialog with an access line that changes wording; `SharePage` gained login / wrong-account / not-found gates and a "Continue in your own chat" action that forks and hands the new thread id to the app through localStorage (no router); "Shared with you" sidebar section; the share route moved from `main.tsx` into `Root` so the login flow is reachable.

**Verified:** 20 new integration tests (access flips, reopening, invited reads, other account 403, unverified address 403, case/padding matching, owner keeps access, list and removal, duplicate invite, malformed address 422, non-owner 404, "shared with you" listing and its 401, fork contents and NULL project and stripped citations, owner thread untouched, stranger 403, anonymous 401, public link stays read-only). Full suite 774 passed; frontend typechecks and builds. Live: public link still open with an empty localStorage, link closing after an invite, the login gate, the wrong-account state, the invited reader view with the continue button, and the owner's invite UI normalizing `Colleague@Example.COM` to `colleague@example.com`.

**Status: complete.** Not built, and worth naming: no email is actually sent to the invitee (they need the link), and a public link is still read-only rather than continuable.

## Project Members: Viewer / Editor / Owner Transfer

**Discussed (2026-08-17):** User asked to add users to projects. Asked what a member may do; the user designed the roles themselves: owner picks a role before sharing, Viewer = view only in the project, Editor = everything except deleting chats and the project, and an Owner handoff that makes the new person owner and drops the old one to Editor. They asked "làm như vậy có được không?" — it is, and it was built as specified.

Two things were flagged before building: an Editor deleting a document deletes it for the whole team (their call, built as asked), and ownership transfer forces a decision about the retrieval tenant.

On that tenant: I first said I would filter by `project_id` alone, then read `_tenant_conditions` and reversed it. Keeping `owner_user_id` in the filter and passing the *project's* owner is a one-line change at the boundary instead of threading `Optional` through ten call sites in `chain.py`, and it keeps a layer of defence. The cost is that a transfer must rewrite the project's Qdrant payload, which reuses the existing, tested mechanism narrowed to one project.

**Implemented (backend):**
- `project_members` (project_id FK CASCADE, normalized email, role, unique per pair, indexed on email). Owner not stored there.
- `ProjectRole` with `at_least()`; `accessible_project(caller, project_id, need=...)` returns `(project, role)`, 404 for outsiders and 403 for under-privileged members.
- Every project route now runs its store and index work as `project.owner_user_id`. `ctx.retrieve` and `owned_document` too. This is the whole reason a member's question finds the owner's documents; there is a test asserting the hit text rather than just a 200, because the failure mode is an empty list, not an error.
- `transfer_owner` swaps ownership and inserts the previous owner as editor in one transaction; the route then calls `QdrantHybridIndex.reassign_project_owner` and invalidates the retrieval cache.
- `users.find_uid_by_email`, and `record_login` now normalizes the stored address.
- Threads: creating a thread in, and chatting in, a shared project goes through `accessible_project`. Threads stay owned by whoever wrote them; a member's chats are not visible to the owner.

**Implemented (frontend):** `role` on `Project` and `roleAtLeast`; member CRUD + transfer in `lib/api.ts`; the Projects "Shared with you" tab filters the one list by role; `ProjectMembersDialog` (owner only) with a two-step confirm on "Make owner"; upload button hidden for viewers; a banner on a shared project saying what the caller may do.

**Verified:** 18 new integration tests (invite with role, case-insensitive match, re-invite re-roles, anonymous owner 403, non-owner 403, bad role 422, unverified address blocked, viewer reads + queries the owner's chunks but cannot rename/upload/delete, editor renames but cannot delete the project, stranger 404, removal revokes, role in the project list for both sides, transfer promotes and demotes, owner-only powers move, retrieval survives a transfer, transfer needs an existing account, only the owner transfers). Full suite 792 passed. Frontend typechecks and builds. Live: migration applied, routes in the OpenAPI, outsider 404s on documents and delete, anonymous invite 403, `role: owner` in the list.

**Status: complete.** Not built: no email is sent to the invitee, and per-member usage accounting still bills whoever asked the question.

## Project Members Dialog: Single Role Control, Deferred Save

**Discussed (2026-08-17):** User asked (with a reference image of a plain "Editor ⌄" text button) to replace the role span plus the separate Make owner and remove buttons with one button showing the current role, opening a menu of Make Owner / Editor / Viewer / Remove Access in red, and to save only when Done is pressed.

Two consequences flagged before building. First, deferring the save makes the ordering matter: a transfer demotes the caller to editor, so it has to run after every other member call or the rest 403s. Second, adding by email was left as the only immediate write in the first draft, which would have meant two different save rules in one dialog; it was folded into the same draft model.

**Implemented:** `ProjectMembersDialog` holds `rows` (email, role, removing, added) plus a single `newOwner`, diffed against the loaded list. `dirty` gates Done. Save runs role upserts, then removals, then the handover, promoting a newly-added target to member first if needed. Picking a plain role or Remove Access clears a pending handover, so two owners is unrepresentable. A removed row stays visible reading "Removing" so the choice is reversible with the same control. Menu flips up against the dialog card's bottom edge.

**Verified:** typecheck and build clean. In the browser against a stubbed member API: menu contents and red Remove Access match the reference, the flip keeps the menu inside the card, staged edits fire **zero** requests before Done, and Done produced exactly `POST members bob editor` followed by `POST transfer alice` with no redundant write for the already-listed new owner.

**Status: complete.**

## Retrieval: Relative Score Floor Instead of an Absolute Threshold

**Discussed (2026-08-17 to 2026-08-18):** Came out of a longer thread on making the chat agent stronger. User asked whether retrieval should become a tool the model calls, then whether a score threshold (they suggested 0.3) could stop weak chunks reaching the prompt, and asked for open-source references.

Three decisions, in order.

First, retrieval stays unconditional and the tool is additive. User rejected replacing the query rewrite plus the retrieve step with a tool alone, on the grounds that the model may be overconfident and skip the call. That is the right read: LangGraph's own agentic-rag reference makes retrieval optional and then has to add `grade_documents` and `rewrite_question` to recover from a weak first pass. Keeping the first retrieve unconditional and adding `search_documents` for follow-up queries gets multi-hop without that safety net, keeps the rewrite, and keeps `[cite:N]` warm-started from a list built before generation. Not built yet.

Second, no absolute threshold and no calibration run. Cohere documents a procedure for picking one (30 to 50 representative queries, a borderline-relevant document each, average the scores) but also documents that the score is query-dependent, which makes any constant it produces wrong by construction. User's objection was sharper: real traffic spans every content type and every query type, so a few dozen queries measures the sample and is very likely biased. Agreed and dropped.

Third, a relative cut needs no number at all. Keep hits scoring at least `keep_ratio` of the best hit. Scale-free, so it survives both the query dependence and the fact that three reranker families are configurable here. Being wrong about the ratio is forgiving in a way that being wrong about 0.3 is not.

One thing was flagged before building and left alone: the token saving is small, because `token_budget=4000`, `route_limit=5` and `reference_cap=5` already bound the evidence. This is a quality change, not an optimization, and the user's stated motivation ("avoid unnecessary context") is not really what it delivers.

**Implemented:** `RetrievalConfig.keep_ratio` (default 0.5, bounded `ge=0.0, le=1.0`, env `RETRIEVAL_KEEP_RATIO`). In `run_retrieval`, after the rerank loop and before the ancestor lookup, `evidence` is the subset scoring at or above `max(scores) * keep_ratio`; `ranked` stays the full scored set. The `le=1.0` bound and a guard on a non-positive top score are what make "never empty" true rather than merely likely.

**Verified:** 5 new tests in `tests/test_retrieval_chain.py`. Full suite 837 passed. One real regression caught and fixed mid-implementation: the first version pruned cut leaves from `scores` and passed the filtered list into the reference weighting, which let a cut leaf's references return at the promoted ancestor's strength and outrank a surviving leaf's. `tests/test_review_round2.py::test_reference_ids_are_truncated_by_weight_before_the_fetch` caught it.

**Status: complete.** Deliberately not built: the absolute floor and the "retrieval is weak" signal it would feed, which is blocked on an eval set (lesson.md #6) and connects to the abstention rule (lesson.md #7). Also not built: the `search_documents` tool itself, and the prompt-shape and caching consequences that follow it.

## Admin Screen: Users, Reports Inbox, Grants, Audit

**Discussed (2026-08-18):** User asked what the admin screen should contain, then said build it. The scope was read off the schema already sitting uncommitted in the working tree: `AdminUserRow` + `AuthConfig.admin_emails` (admins tab), `UserRow.disabled_at` (users tab), `MessageReportRow.resolved_at/resolved_by` (reports inbox), `AdminAuditRow` (audit log). One decision stated up front and not overridden: an admin reads only the conversation snapshot inside a report, never a user's live chats — that would be a different power and nothing in the schema implies it.

**Implemented (backend):**
- Migration `f2b8d4a6c1e9`: `admin_users`, `admin_audit`, `users.disabled_at`, `message_reports.resolved_at/resolved_by`.
- `storage/admin.py` protocol + `storage/pg_admin.py`. One store reads across users, usage, reports and grants because the admin screen is a view over the whole system.
- `UserStore.is_disabled`; the auth middleware checks it live on every Bearer request and answers 403, so a disable locks the account out of every route at once.
- `ApiContext.require_admin()` resolves standing from the *verified* address: bootstrap set from `ADMIN_EMAILS`, then `admin_users`. `routers/admin.py`: `/admin/me`, users list + disable/enable, reports list + resolve, grants list/add/remove, audit list. Bootstrap admins cannot be disabled or revoked (403). Every state change writes one audit row; no-ops and refusals write none.
- `create_query_app(..., admins=, admin_emails=)`, wired from `runtime_config.auth.admin_emails` in `app.py`.

**Implemented (frontend):** `AdminPage.tsx` on `/admin` outside the app shell (same pattern as `SharePage`), four tabs, login-in-place when signed out, polite 403 view. Sidebar profile menu gains an Admin entry, shown only when `GET /admin/me` succeeds — the gate itself stays on the server.

**Also fixed:** `tests/test_migrations.py` still injected `search_path` via the startup packet the pooler drops; against a pooled URL its downgrade test would have dropped every table in `public`. Now uses the same on-connect `SET` as conftest plus a hard assert. Public DB was empty; nothing was lost.

**Verified:** 18 tests in `tests/test_admin_api.py` (403 for normal/anonymous/unverified-claimant, bootstrap listing, disable locks out everywhere + enable restores, bootstrap disable/revoke refused, grant normalizes case and 409s duplicates, revoke shuts out, inbox with conversation snapshot, resolve idempotent with first resolver kept, audit rows in order, refused actions write none). Config test for `ADMIN_EMAILS` parsing. Migration chain + model parity + downgrade green. Frontend typechecks and builds.

**Status: complete.** Not built, on purpose: admin access to live user chats, editing user data, per-user quotas, deleting reports.


## Session Expiry: Silent Auto-Logout

**Discussed (2026-08-24):** User hit "Invalid Firebase ID token" on every request. Diagnosis: the localStorage token had expired and the browser's Firebase session could no longer mint a fresh one, so the retry-once-on-401 in `fetchWithAuth` had nothing to retry with and the app stayed "logged in" with a dead identity. A design with an expiry banner was proposed; the user chose the simpler behavior — log out directly, no notification.

**Implemented:** In `fetchWithAuth`, a 401 that survives the refresh-and-retry path while a token is attached triggers `expireSession()`: clear the token, `resetIdentity()` (same reasoning as the manual logout — a lingering verified uid would be refused by the X-User-ID fallback), Firebase `signOut`, then reload in place so a share-link reader keeps their URL. A module-level flag collapses concurrent 401s into one logout. The normal hourly token refresh is unchanged.

**Verified:** frontend typecheck and build clean. The triggering condition excludes token-less requests, so anonymous browsing and the public share page never trip it.

**Amended (2026-08-24, before deploy):** the user's report of "Lorem Ipsum then it logs in by itself" exposed a startup race: `currentUser` is null while the SDK restores the session, so the first version would have logged healthy users out on slow startups. The 401 path now awaits `authStateReady()` before refreshing or expiring, which also makes the opening burst of requests self-heal instead of failing until a reload.

**Status: complete.**


## Startup Delay: Snapshot Hydration

**Discussed (2026-08-24):** After the token-refresh fixes, ~3s of visible startup delay remained on the user's machine. Measured: the app itself paints in 616ms and the API answers instantly; the cost is the Firebase SDK restoring a cold session over the network (securetoken 0.34-0.66s, identitytoolkit 1.3s). Not removable client-side. User approved caching, asked for the most practical optimization — answer: stale-while-revalidate for the whole sidebar, not just the name.

**Implemented:** `UiSnapshot` in `storage.ts` (userId, displayName, threads, projects), saved on every change while logged in, hydrated into the initial React state on the next start, ignored when the stored userId differs, cleared by `resetIdentity()`. Sidebar display name falls back to the snapshot after live settings and the nickname memory.

**Verified:** typecheck and build clean; fresh-load console clean. Live logged-in verification needs the user's own session.

**Status: complete.** Not cached, on purpose: message content, settings toggles, memories.


## Admin Overview Dashboard

**Discussed (2026-08-25):** User asked for a dashboard with total usage, and what else the admin screen could manage. Built the dashboard from data that already exists; flagged that a usage-over-time chart is blocked on a per-event usage log (`llm_usage` is a lifetime accumulator).

**Implemented:** `AdminStore.stats()` (one read: user counts incl. active-7d and disabled, summed token/request totals, thread/message/project/document/memory counts, failed-document count, report open/resolved, top 5 users by tokens); `GET /admin/stats`; Overview tab with stat tiles + top-users table, now the default tab.

**Verified:** 2 new tests green against seeded data; OpenAPI surface test updated; frontend typechecks and builds.

**Status: complete.**

## Manual Compact: Gauge Re-estimate, Char-capped Folds, Stall Reporting, Instructions

**Discussed (2026-08-24):** Traced `/compact` in both this repo and the claude-code source tree, compared the two designs. This repo's item-based delta merge (retention as a property of code) stays; four gaps around it get fixed. User decisions: (1) fix the gauge now; (2) cap folds by chars AND messages, chars binding, message cap bounds the fetch; (3) accept compact instructions like claude-code's `/compact <args>`; (4) summary viewer deferred pending a concept sketch; (5) evidence-stripping pre-pass (microcompact analog) deferred until 1-3 land.

**Implemented (2026-08-24):**
- `deps.compact_thread` returns `(thread, changed, stalled)`; after a changed run, `estimate_prompt_after_compact` prices floor-window + summary at the thread's measured chars/token and stores it via `record_prompt_metrics`, so the compact response and thread view show the drop immediately. Estimate skipped when no measured turn exists (gauge hidden anyway). Next real turn overwrites the estimate with a measurement; calibration is preserved because chars and tokens are stored at the same ratio.
- `fold_through` trims each fold to `summary_max_fold_chars` (new config, env `SUMMARY_MAX_FOLD_CHARS`, default 32,000); first message unconditional so the watermark always advances. Takes `instructions` and passes them to the summarizer.
- `LLMSummarizer.summarize` gains `instructions`; appended to the EXTRACT user prompt via `EXTRACT_INSTRUCTIONS_TEMPLATE`. CURATE untouched by design.
- `POST /threads/{id}/compact` takes optional `CompactThreadRequest{instructions}`; `CompactThreadResponse` gains `stalled`. Frontend: `types.ts`, `api.ts` optional arg, `App.tsx` toast matrix (folded / partial / failed / nothing).
- Tests: RED first, 6 new (test_thread_window.py x5, test_summarization.py x1), fakes extended (usage callback in `_Chat`, `fail`/`instructions` on `_Summarizer`, `instructions` kwarg on RecordingSummarizer).

**Status:** complete (backend targeted 38 passed; frontend tsc + vite build clean; full backend suite: 857 passed in 3m48s).
**Deferred:** summary viewer UI (concept presented, awaiting user pick), evidence-stripping pre-pass, composer UI for typing compact instructions.
