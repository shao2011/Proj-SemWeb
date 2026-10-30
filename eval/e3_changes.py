"""E3: requirements that change, a five-edit case study.

    DATABASE_URL=postgresql://postgres:password@<host>:5432/rag \
        python -m eval.e3_changes --edits 1 --models nemotron

Runs the system's own FastAPI app in process (no auth, real Postgres, Qdrant
in memory, Jina, the answer model) and drives it through its HTTP API: upload,
thread messages, compaction and the block-edit endpoint.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from eval import settings
from eval.data import RequestaItem, load_requesta, pdf_for
from eval.jina import estimate_tokens, jina_bundle
from eval.judge import Judge
from eval.llm import ChatClient
from eval.metrics import clean_answer, is_relevant
from eval.results import ResultStore

EDITS_FILE = Path(__file__).with_name("e3_edits.json")
OWNER = "eval-harness"
HEADERS = {"X-User-ID": OWNER}
FILLER_EXCHANGES = 3


@dataclasses.dataclass(frozen=True)
class Edit:
    qid: int
    old: str
    new: str


def load_edits(path: Path = EDITS_FILE) -> list[Edit]:
    return [Edit(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def value_pattern(value: str) -> re.Pattern[str]:
    """`value` with any run of whitespace allowed between its words."""
    return re.compile(r"\s+".join(re.escape(part) for part in value.split()))


def choose_block(blocks: Sequence[dict], edit: Edit, gold_passage: str) -> dict:
    """The editable block holding the old value that best matches the gold passage."""
    pattern = value_pattern(edit.old)
    candidates = [b for b in blocks if pattern.search(b["text"])]
    if not candidates:
        raise LookupError(f"No block contains {edit.old!r}")
    relevant = [b for b in candidates if is_relevant(b["text"], gold_passage)]
    return (relevant or candidates)[0]


def replace_value(text: str, edit: Edit) -> str:
    return value_pattern(edit.old).sub(edit.new, text)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None:
            events.append((name, data or {}))
    return events


class RecordingEmbedder:
    """Counts what the app re-embeds; delegates the real work.

    Sends at most `batch` texts per request with bounded retries: the app hands
    a whole document to one call, which times out on a large SRS. Batching
    changes the transport only, not the vectors.
    """

    def __init__(self, delegate, *, batch: int = 16, attempts: int = 5, sleep=time.sleep) -> None:
        self._delegate = delegate
        self._batch = batch
        self._attempts = attempts
        self._sleep = sleep
        self.texts: list[str] = []

    def embed_documents(self, items, profile, *, intent):
        self.texts.extend(items)
        vectors: list[list[float]] = []
        for start in range(0, len(items), self._batch):
            part = list(items[start : start + self._batch])
            for attempt in range(1, self._attempts + 1):
                try:
                    vectors.extend(self._delegate.embed_documents(part, profile, intent=intent))
                    break
                except Exception:
                    if attempt == self._attempts:
                        raise
                    self._sleep(min(2**attempt, 30))
        return vectors

    def embed_query(self, text, profile, *, intent):
        return self._delegate.embed_query(text, profile, intent=intent)


class SystemUnderTest:
    """The app wired the way `create_runtime_app` wires it, minus auth."""

    def __init__(self, client: ChatClient, workdir: Path) -> None:
        os.environ.setdefault("AUTH_ALLOW_UNAUTHENTICATED", "1")
        from fastapi.testclient import TestClient
        from qdrant_client import QdrantClient

        from rag_ingestion.api.routes import create_query_app
        from rag_ingestion.config import ConversationConfig, RuntimeConfig
        from rag_ingestion.db.base import target_metadata
        from rag_ingestion.db.session import build_engine
        from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
        from rag_ingestion.providers.registry import ProviderRegistry
        from rag_ingestion.storage.media import LocalMediaStore
        from rag_ingestion.storage.pg_projects import PostgresProjectStore
        from rag_ingestion.storage.pg_threads import PostgresThreadStore
        from rag_ingestion.summary import LLMSummarizer
        from rag_ingestion.understanding import LLMUnderstander

        runtime = RuntimeConfig.from_environment()
        engine = build_engine(runtime.database_url)
        target_metadata.create_all(engine)
        bundle = jina_bundle(runtime)
        self.embedder = RecordingEmbedder(bundle.embedder)
        self.bundle = dataclasses.replace(bundle, embedder=self.embedder)
        qdrant = QdrantClient(location=":memory:")
        self.index = QdrantHybridIndex(qdrant, self.bundle.collection_name, self.bundle.profile)
        self.threads = PostgresThreadStore(engine)
        conversation = ConversationConfig()
        self.model_id = client.model_id
        app = create_query_app(
            qdrant,
            ProviderRegistry([self.bundle]),
            PostgresProjectStore(engine),
            threads=self.threads,
            chat=client.agent,
            understand=LLMUnderstander(client.agent, max_tokens=conversation.understand_max_tokens),
            summarizer=LLMSummarizer(client.agent, max_tokens=conversation.summary_max_tokens),
            media_store=LocalMediaStore(workdir / "media"),
            conversation=conversation,
            configured_routers=frozenset(name for name, r in runtime.llm.routers.items() if r.api_keys),
        )
        self.http = TestClient(app)

    def project_with(self, pdf: Path) -> tuple[str, str]:
        response = self.http.post("/projects", headers=HEADERS, json={"name": f"e3 {pdf.stem}", "provider": "jina"})
        response.raise_for_status()
        project_id = response.json()["project_id"]
        with pdf.open("rb") as handle:
            upload = self.http.post(
                f"/projects/{project_id}/documents", headers=HEADERS,
                files=[("files", (pdf.name, handle.read(), "application/pdf"))],
            )
        results = [data for name, data in parse_sse(upload.text) if name == "file_result"]
        if not results or results[0].get("status") != "completed":
            raise RuntimeError(f"Upload failed: {upload.text[:300]}")
        return project_id, results[0]["doc_id"]

    def new_thread(self, project_id: str) -> str:
        response = self.http.post("/threads", headers=HEADERS, json={"project_id": project_id, "title": "e3"})
        response.raise_for_status()
        return response.json()["thread_id"]

    def ask(self, thread_id: str, question: str, *, attempts: int = 3) -> dict:
        """One turn through the API. A provider failure leaves the user message
        in the thread without an answer, so a retry repeats the question there."""
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            response = self.http.post(
                f"/threads/{thread_id}/messages", headers=HEADERS,
                json={"content": question, "model_id": self.model_id, "retrieval_limit": settings.RETRIEVAL_LIMIT},
            )
            seconds = time.perf_counter() - started
            events = dict(parse_sse(response.text))
            if "done" in events:
                break
            if attempt == attempts:
                raise RuntimeError(f"No answer: {response.text[:300]}")
            time.sleep(min(2**attempt * 5, 60))
        thread = self.threads.get_thread(owner_user_id=OWNER, thread_id=thread_id)
        return {
            "answer": clean_answer(events["done"]["content"]),
            "citations": [c["chunk_id"] for c in events["done"].get("citations", [])],
            "seconds": seconds,
            "prompt_tokens": thread.last_prompt_tokens if thread else None,
        }

    def pad_and_compact(self, thread_id: str, fillers: Sequence[RequestaItem]) -> bool:
        from rag_ingestion.storage.threads import ThreadMessageRole

        for item in fillers:
            self.threads.add_message(owner_user_id=OWNER, thread_id=thread_id, role=ThreadMessageRole.USER, content=item.question)
            self.threads.add_message(owner_user_id=OWNER, thread_id=thread_id, role=ThreadMessageRole.ASSISTANT, content=item.answer)
        response = self.http.post(f"/threads/{thread_id}/compact", headers=HEADERS, json={})
        response.raise_for_status()
        return bool(response.json().get("changed"))

    def edit(self, project_id: str, doc_id: str, edit: Edit, gold_passage: str) -> dict:
        blocks = self.http.get(f"/projects/{project_id}/documents/{doc_id}/blocks", headers=HEADERS).json()
        block = choose_block(blocks["blocks"], edit, gold_passage)
        self.embedder.texts.clear()
        started = time.perf_counter()
        response = self.http.patch(
            f"/projects/{project_id}/documents/{doc_id}/blocks", headers=HEADERS,
            json={"base_version": blocks["version"], "edits": [{"block_id": block["block_id"], "text": replace_value(block["text"], edit)}]},
        )
        seconds = time.perf_counter() - started
        response.raise_for_status()
        return {
            "chunks": response.json()["embedded"],
            "embedding_tokens": sum(estimate_tokens(t) for t in self.embedder.texts),
            "seconds": seconds,
        }

    def full_reindex(self, project_id: str, doc_id: str) -> dict:
        ids = list(self.index.doc_chunk_parents(owner_user_id=OWNER, project_id=project_id, doc_id=doc_id))
        chunks = list(self.index.fetch_chunks(ids, owner_user_id=OWNER, project_id=project_id).values())
        self.embedder.texts.clear()
        started = time.perf_counter()
        for start in range(0, len(chunks), 96):
            self.index.upsert(chunks[start : start + 96], self.embedder)
        return {
            "chunks": len(chunks),
            "embedding_tokens": sum(estimate_tokens(t) for t in self.embedder.texts),
            "seconds": time.perf_counter() - started,
        }

    def cited_text(self, project_id: str, chunk_ids: Sequence[str]) -> str:
        found = self.index.fetch_chunks(list(chunk_ids), owner_user_id=OWNER, project_id=project_id)
        return "\n".join(chunk.source_text for chunk in found.values())


def run(edit_limit: int | None, model_keys: Sequence[str], judge: bool) -> None:
    items = {item.qid: item for item in load_requesta()}
    edits = load_edits()[:edit_limit] if edit_limit else load_edits()
    models = [m for m in settings.ANSWER_MODELS if any(key in m.model_id for key in model_keys)]
    store = ResultStore(settings.RESULTS_DIR / "e3" / "case_study.jsonl")
    judge_client = Judge() if judge else None
    for model in models:
        client = ChatClient(model.model_id, model.router)
        system = SystemUnderTest(client, Path(tempfile.mkdtemp(prefix="e3-")))
        for edit in edits:
            if store.has(("answer", model.model_id, edit.qid, "S3")):
                continue
            item = items[edit.qid]
            fillers = [other for other in items.values() if other.document == item.document and other.qid != item.qid][:FILLER_EXCHANGES]
            project_id, doc_id = system.project_with(pdf_for(item.document))
            same_thread = system.new_thread(project_id)
            store.put(("before", model.model_id, edit.qid, "S2"), {"kind": "before", **system.ask(same_thread, item.question)})
            compacted = system.new_thread(project_id)
            store.put(("before", model.model_id, edit.qid, "S3"), {"kind": "before", **system.ask(compacted, item.question)})
            folded = system.pad_and_compact(compacted, fillers)
            cost = system.edit(project_id, doc_id, edit, item.gold_passage)
            if not store.has(("cost", edit.qid)):
                store.put(("cost", edit.qid), {"kind": "cost", "qid": edit.qid, "partial": cost, "full": system.full_reindex(project_id, doc_id)})
            threads = {"S1": system.new_thread(project_id), "S2": same_thread, "S3": compacted}
            for situation in settings.E3_SITUATIONS:
                reply = system.ask(threads[situation], item.question)
                row = {"kind": "answer", "model": model.model_id, "qid": edit.qid, "situation": situation,
                       "old": edit.old, "new": edit.new, "compacted": folded, **reply}
                if judge_client:
                    row["label"] = judge_client.e3(
                        question=item.question, old_value=edit.old, new_value=edit.new,
                        cited=system.cited_text(project_id, reply["citations"]), answer=reply["answer"],
                    )
                store.put(("answer", model.model_id, edit.qid, situation), row)
    print(json.dumps([row for row in store.rows() if row.get("kind") in ("answer", "cost")], indent=1, ensure_ascii=False)[:4000])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E3 requirement edits (case study)")
    parser.add_argument("--edits", type=int, default=None, help="First N edits (smoke runs)")
    parser.add_argument("--models", nargs="+", default=["nemotron", "hy4"])
    parser.add_argument("--no-judge", action="store_true")
    options = parser.parse_args(argv)
    run(options.edits, options.models, judge=not options.no_judge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
