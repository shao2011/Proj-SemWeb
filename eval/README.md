# Evaluation harness

Code for three experiments on the RAG system in `backend/`. Every setting is
in [`settings.py`](settings.py) and the experiments read it from there only.

| Experiment | Script | Data | What runs |
|---|---|---|---|
| E1 chunking | `e1_chunking.py` | REQuestA (159 questions) | C1 fixed, C2 semantic, C3 structure + heading prefix (Auto-Merge off), C4 late chunking, C5 = C3 + Auto-Merge (system default) |
| E2 long conversations | `e2_history.py` | LoCoMo (10 conversations, 1,986 questions) | H1 full, H2 sliding window, H3 recursive summary, H4 history retrieval, H5 itemised fold (system), LoCoMo-RAG, Mem0, LangMem |
| E3 changing requirements | `e3_changes.py` | REQuestA, 5 edits in `e3_edits.json` | the system's FastAPI app in process: upload, threads, compaction, block edit |

What is the system's own code and what is not:

- C3/C5 chunking, retrieval, reranking, prompt building, H5 summarizer, the
  history window (`rag_ingestion.history_window`), the upload, edit and
  compaction endpoints: the system's code, called directly.
- C1, C2, C4, H3, H4 and the H1-H5 replay loop: written here. The replay
  mirrors `ApiContext.preflight_turns` step by step with the system's window
  function and constants.
- Mem0 and LangMem: their official libraries, using the same answer model and
  Jina embeddings.

## Setup

```bash
uv sync --extra test --extra eval          # mem0ai, langmem, nltk
```

Keys come from the repo `.env`: `JINA_API_KEY`, `OPENROUTER_API_KEY`
(Nemotron 3 Ultra and the GPT-4o judge), `ORCAROUTER_API_KEY` (Hy4 preview).
The GPT-4o judge is a paid model: the OpenRouter account needs credit.
REQuestA must be in `data/REQuestA` (see its README). LoCoMo downloads itself
to `data/LoCoMo` on first use.

E3 needs Postgres (`docker compose up -d db`). On this machine a Homebrew
Postgres shadows `localhost:5432`, so point `DATABASE_URL` at the LAN IP:

```bash
export DATABASE_URL="postgresql://postgres:password@$(ipconfig getifaddr en0):5432/rag"
```

## Run

```bash
.venv/bin/python -m eval.e1_chunking                     # all of E1
.venv/bin/python -m eval.e2_history                      # all of E2
.venv/bin/python -m eval.e3_changes                      # all of E3
```

Useful flags: `--models nemotron hy4`, `--configs`/`--methods`, `--no-judge`,
and for smoke runs `--limit N` (E1), `--conversations N --questions N
--max-messages N` (E2), `--edits N` (E3). `--max-messages` truncates
conversations and is for smoke runs only, never for final numbers.

Results are appended to `eval/results/<experiment>/*.jsonl` (git-ignored; set
`EVAL_RESULTS_DIR` to write elsewhere). A rerun skips rows already stored, so
a run stopped by a rate limit resumes where it stopped. Each script prints
the table cells at the end: means of F1, BLEU-1, prompt tokens and seconds,
and label shares on the judged sample.

## Tests

```bash
.venv/bin/python -m pytest eval/tests -q
```

No network and no Postgres needed: fake embedders and chat models.

## Known limits

- The judged sample is fixed by `SAMPLE_SEED`; the 50 E1 questions, the 50
  answered and the 50 adversarial LoCoMo questions are the same on every run.
- Free models can be slow or rate-limited. Calls retry with backoff; E3 also
  retries a turn whose answer failed, which repeats the question in that
  thread.
- E3 sends embeddings in batches of 16. The app itself sends a whole document
  in one request, which timed out on the 26-page LunarRover SRS.
