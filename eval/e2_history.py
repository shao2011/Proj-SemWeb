"""E2: long conversations on LoCoMo.

    python -m eval.e2_history --methods H2 H5 --models nemotron --conversations 1 --questions 3

H1 to H5 replay a conversation as thread history. The window and the folding
follow the system's own rules (`rag_ingestion.history_window`, the same
refill-and-fold steps as `ApiContext.preflight_turns`), and H5 uses the system's
`LLMSummarizer`. LoCoMo-RAG, Mem0 and LangMem answer from retrieved memories.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from qdrant_client import QdrantClient

from rag_ingestion.chat import ChatTurn
from rag_ingestion.chunking import Chunk, ChunkLevel
from rag_ingestion.config import ConversationConfig, RuntimeConfig
from rag_ingestion.history_window import HISTORY_REFILL_RATIO, as_turn, plan_history
from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex, chunk_from_payload
from rag_ingestion.retrieval.answering import build_turns
from rag_ingestion.schemas.answering import AnswerEvidence
from rag_ingestion.storage.threads import ThreadMessage, ThreadMessageRole
from rag_ingestion.summary import LLMSummarizer
from scripts.benchmark.mtrag_run import index_corpus

from eval import settings
from eval.data import LocomoConversation, LocomoQuestion, fetch_locomo, load_locomo
from eval.jina import LangChainJina, jina_bundle
from eval.judge import Judge, sample
from eval.llm import ChatClient
from eval.metrics import bleu1, clean_answer, locomo_f1
from eval.results import ResultStore, summarise

OWNER = "eval-harness"
_EPOCH = datetime(2023, 1, 1, tzinfo=timezone.utc)

# H3: the summary prompt the system used before H5 (prompts/summarization.py,
# backup of 2026-08-15), with its length rule set to the H5 budget.
RECURSIVE_SUMMARY_PROMPT = f"""You keep a running summary of the earlier part of a chat, so those turns can be dropped from the prompt without losing what happened.

You will be given the summary so far (if any) followed by the turns that have just scrolled out of the visible window. Fold the new turns into the summary and return the merged result.

Output ONLY the summary text. No preamble, no markdown headings, no bullet list, no quotes around it.

Rules:
1. Record what was discussed and what was answered. Nothing else.
2. Do not carry over instructions, preferences, or standing requests ("answer in English", "keep it short"). Those are stored separately and repeating them here would apply them twice.
3. Keep every concrete detail that a later question could refer back to: names, numbers, article and section identifiers, decisions reached, and anything the user said about their own situation.
4. Write in the language the conversation is in.
5. Stay under {settings.RECURSIVE_SUMMARY_MAX_CHARS} characters. Compress the oldest material first when you run out of room, since the newest turns are the ones a follow-up question is most likely to touch.
6. Never invent anything that is not in the summary or the turns you were given. If the turns are trivial, return a short summary rather than padding it."""


def _turn_lines(turns: Sequence[ChatTurn]) -> str:
    return "\n".join(f"{turn.role}: {turn.content}" for turn in turns)


class RecursiveSummarizer:
    """H3: rewrites one summary paragraph on every fold."""

    def __init__(self, complete: Callable[[list[ChatTurn]], str]) -> None:
        self._complete = complete

    def summarize(self, *, previous, turns, **_ignored) -> str | None:
        body = f"Turns that just scrolled out:\n{_turn_lines(turns)}"
        if previous:
            body = f"Summary so far:\n{previous}\n\n{body}"
        text = self._complete(
            [ChatTurn(role="system", content=RECURSIVE_SUMMARY_PROMPT), ChatTurn(role="user", content=body)]
        ).strip()
        return text[: settings.RECURSIVE_SUMMARY_MAX_CHARS] or previous


def thread_messages(conversation: LocomoConversation) -> list[ThreadMessage]:
    return [
        ThreadMessage(
            message_id=f"{conversation.conversation_id}-{m.dia_id}",
            thread_id=conversation.conversation_id,
            role=ThreadMessageRole.USER if m.role == "user" else ThreadMessageRole.ASSISTANT,
            content=m.content,
            created_at=_EPOCH,
            position=index,
        )
        for index, m in enumerate(conversation.messages, start=1)
    ]


@dataclass
class FoldingThread:
    """The system's window-and-fold loop for one thread, without Postgres.

    Same steps as `ApiContext.preflight_turns` and `fold_through`: plan the
    window; on overflow re-plan at the refill ratio and fold the dropped span,
    at most `FOLD_MAX_MESSAGES` messages and `FOLD_MAX_CHARS` characters per
    fold, never folding the opening message.
    """

    summarizer: object | None
    cap: int = settings.HISTORY_WINDOW_MESSAGES
    floor: int = ConversationConfig().history_keep_recent
    budget_chars: int = 10**9
    summary: str | None = None
    watermark: int = 0
    folds: int = 0
    fold_sizes: list[int] = field(default_factory=list)

    def window(self, prior: list[ThreadMessage]) -> list[ThreadMessage]:
        kept, boundary = plan_history(prior, floor=self.floor, cap=self.cap, budget_chars=self.budget_chars)
        if boundary > self.watermark and self.summarizer is not None:
            refill_cap = max(self.floor, int(self.cap * HISTORY_REFILL_RATIO))
            refill_budget = int(self.budget_chars * HISTORY_REFILL_RATIO)
            kept, boundary = plan_history(prior, floor=self.floor, cap=refill_cap, budget_chars=refill_budget)
            self._fold_through(prior, boundary)
            kept, _ = plan_history(prior, floor=self.floor, cap=refill_cap, budget_chars=refill_budget)
        return kept

    def _fold_through(self, prior: list[ThreadMessage], through: int) -> None:
        if through <= self.watermark:
            return
        start = max(self.watermark, 1)
        fresh = [m for m in prior if start < m.position <= through][: settings.FOLD_MAX_MESSAGES]
        trimmed: list[ThreadMessage] = []
        spent = 0
        for message in fresh:
            if trimmed and spent + len(message.content) > settings.FOLD_MAX_CHARS:
                break
            trimmed.append(message)
            spent += len(message.content)
        if not trimmed:
            return
        merged = self.summarizer.summarize(previous=self.summary, turns=[as_turn(m) for m in trimmed])
        if not merged or merged == self.summary:
            return
        self.summary = merged
        self.watermark = trimmed[-1].position
        self.folds += 1
        self.fold_sizes.append(len(trimmed))

    def replay(self, messages: list[ThreadMessage]) -> None:
        """Run the window before every user message, as the system does on each send."""
        for index, message in enumerate(messages):
            if message.role is ThreadMessageRole.USER and index:
                self.window(messages[:index])


def _chunk(conversation_id: str, unit_id: str, text: str, project: str) -> Chunk:
    return Chunk(
        chunk_id=f"{conversation_id}-{unit_id}",
        doc_id=conversation_id,
        owner_user_id=OWNER,
        project_id=project,
        level=ChunkLevel.LEAF,
        source_text=text,
        index_text=text,
        source_block_ids=[unit_id],
        locators=[],
        confidence=1.0,
        citable=True,
    )


class UnitIndex:
    """Hybrid (dense + BM25) search over messages or observations of one
    conversation, with the system's Qdrant index."""

    def __init__(self, bundle, conversation_id: str, units: Sequence[tuple[str, str]], project: str) -> None:
        self._bundle = bundle
        self._project = project
        self.index = QdrantHybridIndex(QdrantClient(location=":memory:"), f"e2-{project}", bundle.profile)
        self.index.ensure_collection()
        index_corpus(self.index, [_chunk(conversation_id, uid, text, project) for uid, text in units], bundle.embedder, quiet=True)

    def search(self, query: str, limit: int) -> list[Chunk]:
        points = self.index.hybrid_search(
            query, self._bundle.embedder, owner_user_id=OWNER, project_id=self._project, limit=limit
        )
        return [chunk_from_payload(point.payload) for point in points]


def retrieved_history(
    hits: Sequence[Chunk], window: Sequence[ThreadMessage], messages: Sequence[ThreadMessage], k: int
) -> list[ThreadMessage]:
    """H4: the top-k earlier messages outside the window, in conversation order."""
    in_window = {m.message_id for m in window}
    by_id = {m.message_id: m for m in messages}
    picked = [by_id[h.chunk_id] for h in hits if h.chunk_id in by_id and h.chunk_id not in in_window][:k]
    return sorted(picked, key=lambda m: m.position)


@dataclass
class Prepared:
    """What a method hands the answer step for one question."""

    history: list[ChatTurn]
    summary: str | None
    evidence: list[AnswerEvidence]


def _memory_evidence(texts: Sequence[str], prefix: str) -> list[AnswerEvidence]:
    return [AnswerEvidence(chunk_id=f"{prefix}-{i}", source_text=t, breadcrumb=prefix) for i, t in enumerate(texts) if t]


class Mem0Memory:
    def __init__(self, client: ChatClient, bundle, conversation_id: str) -> None:
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        from mem0 import Memory

        stores = settings.RESULTS_DIR / "e2" / "mem0-stores"
        stores.mkdir(parents=True, exist_ok=True)
        root = tempfile.mkdtemp(prefix=f"{conversation_id}-", dir=stores)
        self._user = conversation_id
        self._memory = Memory.from_config(
            {
                "llm": {"provider": "langchain", "config": {"model": client.langchain_model()}},
                "embedder": {"provider": "langchain", "config": {"model": LangChainJina(bundle)}},
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": f"mem0-{conversation_id}",
                        "embedding_model_dims": bundle.profile.dimensions,
                        "path": root,
                    },
                },
                "history_db_path": os.path.join(root, "history.db"),
            }
        )

    def add_session(self, messages: Sequence[ThreadMessage]) -> None:
        self._memory.add([{"role": m.role.value, "content": m.content} for m in messages], user_id=self._user)

    def search(self, query: str) -> list[str]:
        found = self._memory.search(query, user_id=self._user, limit=settings.MEMORY_TOP_K, rerank=False)
        return [item["memory"] for item in found.get("results", [])][: settings.MEMORY_TOP_K]


class LangMemMemory:
    def __init__(self, client: ChatClient, bundle, conversation_id: str) -> None:
        from langgraph.store.memory import InMemoryStore
        from langmem import create_memory_store_manager

        self._namespace = ("memories", conversation_id)
        self._store = InMemoryStore(index={"dims": bundle.profile.dimensions, "embed": LangChainJina(bundle)})
        self._manager = create_memory_store_manager(
            client.langchain_model(), namespace=self._namespace, store=self._store
        )

    def add_session(self, messages: Sequence[ThreadMessage]) -> None:
        self._manager.invoke({"messages": [{"role": m.role.value, "content": m.content} for m in messages]})

    def search(self, query: str) -> list[str]:
        items = self._store.search(self._namespace, query=query, limit=settings.MEMORY_TOP_K)
        return [str(item.value.get("content", item.value)) for item in items]


def prepare_method(method: str, conversation: LocomoConversation, client: ChatClient, bundle):
    """Build a method's state for one conversation; returns `question -> Prepared`."""
    messages = thread_messages(conversation)
    conversation_id = conversation.conversation_id
    if method == "H1":
        history = [as_turn(m) for m in messages]
        return lambda question: Prepared(history, None, [])
    if method in ("H2", "H4"):
        thread = FoldingThread(summarizer=None)
        window = thread.window(messages)
        if method == "H2":
            return lambda question: Prepared([as_turn(m) for m in window], None, [])
        index = UnitIndex(bundle, conversation_id, [(m.dia_id, m.content) for m in conversation.messages], "h4")
        limit = settings.HISTORY_RETRIEVAL_TOP_K + len(window)

        def h4(question: str) -> Prepared:
            earlier = retrieved_history(index.search(question, limit), window, messages, settings.HISTORY_RETRIEVAL_TOP_K)
            return Prepared([as_turn(m) for m in [*earlier, *window]], None, [])

        return h4
    if method in ("H3", "H5"):
        if method == "H5":
            summarizer = LLMSummarizer(client.agent, max_tokens=ConversationConfig().summary_max_tokens)
        else:
            summarizer = RecursiveSummarizer(lambda turns: client.complete(turns).text)
        thread = FoldingThread(summarizer=summarizer)
        thread.replay(messages)
        window = thread.window(messages)
        return lambda question: Prepared([as_turn(m) for m in window], thread.summary, [])
    if method == "LoCoMo-RAG":
        # Dated like the replayed messages, so time questions stay answerable.
        units = [(f"obs{i}", f"[{o.date}] {o.text}" if o.date else o.text) for i, o in enumerate(conversation.observations)]
        index = UnitIndex(bundle, conversation_id, units, "locomo-rag")
        return lambda question: Prepared(
            [], None, _memory_evidence([c.source_text for c in index.search(question, settings.MEMORY_TOP_K)], "observation")
        )
    if method in ("Mem0", "LangMem"):
        memory = (Mem0Memory if method == "Mem0" else LangMemMemory)(client, bundle, conversation_id)
        by_session: dict[int, list[ThreadMessage]] = {}
        for source, message in zip(conversation.messages, messages, strict=True):
            by_session.setdefault(source.session, []).append(message)
        for session in sorted(by_session):
            memory.add_session(by_session[session])
        return lambda question: Prepared([], None, _memory_evidence(memory.search(question), "memory"))
    raise ValueError(method)


def score_row(question: LocomoQuestion, answer: str) -> dict:
    if question.category == settings.LOCOMO_ADVERSARIAL_CATEGORY:
        return {}
    return {
        "f1": locomo_f1(answer, question.answer, category=question.category),
        "bleu1": bleu1(answer, question.answer),
    }


def run(
    methods: Sequence[str],
    model_keys: Sequence[str],
    conversation_limit: int | None,
    question_limit: int | None,
    judge: bool,
    max_messages: int | None = None,
) -> None:
    runtime = RuntimeConfig.from_environment()
    bundle = jina_bundle(runtime)
    conversations = load_locomo(fetch_locomo())
    all_questions = [q for c in conversations for q in c.questions]
    judged_answered = set(sample([q.qid for q in all_questions if q.category in settings.LOCOMO_ANSWERED_CATEGORIES]))
    judged_adversarial = set(sample([q.qid for q in all_questions if q.category == settings.LOCOMO_ADVERSARIAL_CATEGORY]))
    if conversation_limit:
        conversations = conversations[:conversation_limit]
    if max_messages:
        for conversation in conversations:
            conversation.messages = conversation.messages[:max_messages]
            sessions = {m.session for m in conversation.messages}
            conversation.observations = [o for o in conversation.observations if o.session in sessions]
    models = [m for m in settings.ANSWER_MODELS if any(key in m.model_id for key in model_keys)]
    judge_client = Judge() if judge else None

    for method in methods:
        store = ResultStore(settings.RESULTS_DIR / "e2" / f"{method}.jsonl")
        for model in models:
            client = ChatClient(model.model_id, model.router)
            for conversation in conversations:
                questions = conversation.questions[:question_limit] if question_limit else conversation.questions
                todo = [q for q in questions if not store.has(("answer", model.model_id, q.qid))]
                if not todo:
                    continue
                prepare = prepare_method(method, conversation, client, bundle)
                for question in todo:
                    prepared = prepare(question.question)
                    turns = build_turns(
                        question.question, prepared.evidence, history=prepared.history, summary=prepared.summary
                    )
                    reply = client.complete(turns)
                    answer = clean_answer(reply.text)
                    row = {
                        "kind": "answer", "model": model.model_id, "qid": question.qid,
                        "category": question.category, "answer": answer,
                        "prompt_tokens": reply.prompt_tokens, "seconds": reply.seconds,
                        **score_row(question, answer),
                    }
                    if judge_client and question.qid in judged_answered:
                        row["label"] = judge_client.e2(question=question.question, reference=question.answer, answer=answer)
                    if judge_client and question.qid in judged_adversarial:
                        row["adversarial_label"] = judge_client.e2_adversarial(question=question.question, answer=answer)
                    store.put(("answer", model.model_id, question.qid), row)
        print(method, summarise(store.rows()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E2 long conversations on LoCoMo")
    parser.add_argument("--methods", nargs="+", default=list(settings.E2_METHODS), choices=settings.E2_METHODS)
    parser.add_argument("--models", nargs="+", default=["nemotron", "hy4"])
    parser.add_argument("--conversations", type=int, default=None, help="First N conversations (smoke runs)")
    parser.add_argument("--questions", type=int, default=None, help="First N questions per conversation")
    parser.add_argument("--max-messages", type=int, default=None, help="Truncate each conversation (smoke runs only)")
    parser.add_argument("--no-judge", action="store_true")
    options = parser.parse_args(argv)
    run(options.methods, options.models, options.conversations, options.questions,
        judge=not options.no_judge, max_messages=options.max_messages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
