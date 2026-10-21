"""Shared state and helpers behind every router.

`ApiContext` bundles the stores, providers, and configs `create_query_app`
was given; routers receive one and call its methods instead of closing over
module globals. Everything that needs no context (SSE framing, header
normalization) stays a plain function.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field

from fastapi import HTTPException
from qdrant_client import QdrantClient

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.config import ChunkingConfig, ConversationConfig, UploadConfig
from rag_ingestion.logger import get_logger
from rag_ingestion.memory.routing import normalize, should_promote
from rag_ingestion.model_catalog import context_percent, load_catalog, usable_tokens
from rag_ingestion.schemas.model_catalog import ModelCatalog, ModelSpec
from rag_ingestion.retrieval import RetrievalConfig, RetrievalOutcome, run_retrieval
from rag_ingestion.memory import build_memory_tools
from rag_ingestion.retrieval.answering import Answerer
from rag_ingestion.retrieval.cache import RetrievalCache
from rag_ingestion.schemas.answering import AnswerEvidence
from rag_ingestion.schemas.understanding import MemoryCandidate, Understanding
from rag_ingestion.summary import Summarizer, parse_items, render_items
from rag_ingestion.understanding import Understander
from rag_ingestion.ingestion.parsers.datalab import DocumentConverter
from rag_ingestion.ingestion.parsers.pdf import ScanParser
from rag_ingestion.storage.media import MediaStore
from rag_ingestion.storage.admin import AdminStore
from rag_ingestion.storage.users import UserStore
from rag_ingestion.memory.models import (
    Memory,
    MemoryScope,
    MemorySource,
    MemoryStore,
    MemoryType,
)
from rag_ingestion.storage.projects import (
    FileProcessingStatus,
    Project,
    ProjectFile,
    ProjectRole,
    ProjectStore,
)
from rag_ingestion.storage.threads import (
    Thread,
    ThreadMessage,
    ThreadMessageRole,
    ThreadStore,
)
from rag_ingestion.providers.registry import (
    ProviderBundle,
    ProviderError,
    ProviderRegistry,
)
from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
from rag_ingestion.api.schemas import QueryHit, QueryRequest, QueryResponse
from rag_ingestion.api.auth import authenticated_email, authenticated_user_id

_log = get_logger(__name__)


def user_id(value: str) -> str:
    verified = authenticated_user_id()
    if verified is not None:
        return verified
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="X-User-ID must not be blank")
    return normalized


def sse(event: str, payload: dict[str, object]) -> str:
    # ensure_ascii=False keeps non-Latin text readable on the wire, which
    # matters when the stream is being read by eye through curl. SSE is
    # UTF-8 by spec and StreamingResponse encodes accordingly.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def _as_turn(message: ThreadMessage) -> ChatTurn:
    return ChatTurn(
        role="user" if message.role is ThreadMessageRole.USER else "assistant",
        content=message.content,
    )


#: Conservative default before any measured turn. Vietnamese runs ~2.5
#: chars/token -- lower than English's ~4 -- so the default over-estimates
#: token counts, which errs toward folding early rather than overflowing.
DEFAULT_CHARS_PER_TOKEN = 2.5

#: After a forced fold, the window refills only to this share of its caps.
#: The freed headroom absorbs the next several turns; without it a thread
#: past its cap would pay a summarizer call on every single message.
HISTORY_REFILL_RATIO = 0.7


def plan_history(
    messages: list[ThreadMessage],
    *,
    floor: int,
    cap: int,
    budget_chars: int,
) -> tuple[list[ThreadMessage], int]:
    """Pick the verbatim window: the opener plus the newest suffix that fits.

    Two ceilings and one floor. The suffix stops at `cap` messages or when
    `budget_chars` is spent, whichever bites first -- but never below `floor`
    messages, because a reply needs its immediate context even when the
    budget disagrees. The opening turn always rides along: it is where the
    subject of the thread is established, and losing it makes every later
    pronoun ambiguous.

    Returns `(kept, boundary)` where `boundary` is the highest position that
    fell out of the window -- 0 when nothing did -- which is exactly how far
    the summary must reach for the prompt to carry the whole conversation.
    """
    if not messages:
        return [], 0
    opener, middle = messages[0], messages[1:]
    if not middle:
        return [opener], 0
    selected: list[ThreadMessage] = []
    spent = len(opener.content)
    for message in reversed(middle):
        over_count = len(selected) >= cap
        over_chars = spent + len(message.content) > budget_chars
        if len(selected) >= floor and (over_count or over_chars):
            break
        selected.append(message)
        spent += len(message.content)
    selected.reverse()
    boundary = selected[0].position - 1 if selected else middle[-1].position
    # The opener itself never counts as fallen out.
    return [opener, *selected], max(0, boundary if boundary > opener.position else 0)


def context_evidence(outcome: RetrievalOutcome) -> list[AnswerEvidence]:
    """What the LLM reads: the auto-merged context, not the leaf fragments.

    A merged parent is one contiguous block in document order; its leaves
    stay the unit for hits and citations. Falls back to the resolved
    leaves for outcomes cached before `context_chunks` was populated.
    """
    chunks = outcome.context_chunks or outcome.chunks
    return [
        AnswerEvidence(
            chunk_id=chunk.chunk_id,
            source_text=chunk.source_text,
            breadcrumb=chunk.index_text.split("\n", 1)[0],
        )
        for chunk in chunks
    ]


def to_response(request: QueryRequest, outcome: RetrievalOutcome) -> QueryResponse:
    return QueryResponse(
        query=request.query,
        hits=[
            QueryHit.from_chunk(chunk, outcome.scores.get(chunk.chunk_id, 0.0))
            for chunk in outcome.chunks
        ],
        evidence_leaf_ids=outcome.evidence_leaf_ids,
        routed_doc_ids=outcome.routed_doc_ids,
        diagnostics=outcome.diagnostics,
    )


@dataclass(frozen=True)
class ApiContext:
    qdrant: QdrantClient
    registry: ProviderRegistry
    projects: ProjectStore
    answerer: Answerer
    conversation: ConversationConfig
    chunking: ChunkingConfig
    upload: UploadConfig
    upload_executor: Executor
    media_store: MediaStore | None
    scan_parser: ScanParser | None
    retrieval: RetrievalConfig
    retrieval_cache: RetrievalCache | None
    threads: ThreadStore | None
    chat: ChatCompleter | None
    memory: MemoryStore | None
    understand: Understander | None
    summarizer: Summarizer | None
    #: Renders a document and reads it back, for formats whose meaning is
    #: carried by pictures rather than by text. Optional: without it a deck
    #: still parses, from its shape tree.
    converter: DocumentConverter | None = None
    users: UserStore | None = None
    admins: AdminStore | None = None
    #: Bootstrap administrators from `ADMIN_EMAILS`, already normalized. The
    #: floor under the admin screen: they need no database row and cannot be
    #: revoked or disabled through the API.
    admin_emails: tuple[str, ...] = ()
    #: Bundled chat-model catalog. A default rather than a wiring parameter:
    #: it is read from a packaged file, so every construction gets the same one.
    models: ModelCatalog = field(default_factory=load_catalog)
    #: Router names that hold API keys, or None where that is not known
    #: (direct `create_query_app` construction, tests). None disables the
    #: router filtering so the picker trusts the catalog alone.
    routers: frozenset[str] | None = None

    # --- ownership checks ---------------------------------------------------

    def require_admin(self) -> str:
        """The caller's verified address, or 403.

        Admin standing hangs off the *verified* email, never the uid: a
        bootstrap admin exists only as an address in configuration, and a
        granted admin may be appointed before they ever sign in. An unverified
        claim to an admin's address is exactly the attack, so it gets nothing.
        """
        email = authenticated_email()
        if email is not None:
            if email in self.admin_emails:
                return email
            if self.admins is not None and self.admins.is_admin(email):
                return email
        raise HTTPException(status_code=403, detail="Administrator access required")

    def admin_store(self) -> AdminStore:
        if self.admins is None:
            raise HTTPException(status_code=503, detail="Admin storage is unavailable")
        return self.admins

    def owned_project(self, owner_user_id: str, project_id: str) -> Project:
        project = self.projects.get_project(
            owner_user_id=owner_user_id,
            project_id=project_id,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    def project_role(self, caller_user_id: str, project: Project) -> ProjectRole | None:
        """What this caller may do in this project, or None for an outsider.

        Ownership is decided by uid; membership by the *verified* address on
        the caller's token. An unverified address resolves to no membership at
        all, because anyone could sign up claiming it.
        """
        if project.owner_user_id == caller_user_id:
            return ProjectRole.OWNER
        email = authenticated_email()
        if email is None:
            return None
        return self.projects.member_role(project_id=project.project_id, email=email)

    def accessible_project(
        self,
        caller_user_id: str,
        project_id: str,
        *,
        need: ProjectRole = ProjectRole.VIEWER,
    ) -> tuple[Project, ProjectRole]:
        """The project plus the caller's role, refusing anything below `need`.

        404 for an outsider and 403 for a member who is not allowed: whether a
        project exists is not an outsider's business, but a member already
        knows, so hiding it from them would just be confusing.

        Every caller must then run its store and index work as
        `project.owner_user_id`, not as the caller: chunks and files carry the
        owner's tenant, so a member acting as themselves would silently see an
        empty project.
        """
        project = self.projects.get_project_by_id(project_id=project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        role = self.project_role(caller_user_id, project)
        if role is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if not role.at_least(need):
            raise HTTPException(
                status_code=403, detail=f"This action needs the {need.value} role"
            )
        return project, role

    def owned_document(
        self, owner_user_id: str, project_id: str, doc_id: str
    ) -> ProjectFile:
        """The file record behind a doc_id, or 404 if it is not this user's.

        Media keys are guessable -- `doc_id` is a hash of content the uploader
        already has -- so serving one without this check would hand a document
        to anyone who could reproduce the file.
        """
        project, _role = self.accessible_project(owner_user_id, project_id)
        # Trashed documents stay readable: their media and markdown are kept
        # for the restore window, and citations in old messages still point
        # here.
        records = (
            self.projects.list_files(
                # The project's owner, not the caller: a member reads the
                # owner's files, and their own id would match nothing.
                owner_user_id=project.owner_user_id,
                project_id=project_id,
                include_trashed=True,
            )
            or []
        )
        for record in records:
            if record.doc_id == doc_id:
                return record
        raise HTTPException(status_code=404, detail="Document not found")

    def owned_thread(self, owner_user_id: str, thread_id: str) -> Thread:
        thread = self.thread_store().get_thread(
            owner_user_id=owner_user_id, thread_id=thread_id
        )
        if thread is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return thread

    def context_used_percent(self, thread: Thread) -> int | None:
        """The gauge value for one thread, or None before any measured turn.

        Computed here, against the budget of the model that produced the
        measurement, so the frontend renders a number instead of re-deriving
        budgets from raw token counts. An unknown or retired model id falls
        back to the default model's budget rather than hiding the gauge.
        """
        spec = self.models.resolve(thread.last_model_id)
        return context_percent(thread.last_prompt_tokens, spec)

    # --- configured-or-503 accessors -----------------------------------------

    def project_bundle(self, project: Project) -> ProviderBundle:
        try:
            bundle = self.registry.get(project.embedding_profile.provider)
        except (ProviderError, ValueError) as error:
            raise HTTPException(
                status_code=503, detail="Project provider is unavailable"
            ) from error
        if bundle.profile.model_dump() != project.embedding_profile.model_dump():
            raise HTTPException(
                status_code=503, detail="Project embedding profile mismatch"
            )
        return bundle

    def thread_store(self) -> ThreadStore:
        if self.threads is None:
            raise HTTPException(status_code=503, detail="Thread storage is unavailable")
        return self.threads

    def chat_provider(self) -> ChatCompleter:
        if self.chat is None:
            raise HTTPException(
                status_code=503, detail="Chat provider is not configured"
            )
        return self.chat

    def memory_store(self) -> MemoryStore:
        if self.memory is None:
            raise HTTPException(status_code=503, detail="Memory storage is unavailable")
        return self.memory

    # --- retrieval ------------------------------------------------------------

    def retrieve(
        self,
        request: QueryRequest,
        *,
        owner_user_id: str,
        project: Project,
        rewritten_query: str | None = None,
    ) -> RetrievalOutcome:
        bundle = self.project_bundle(project)
        # Everything below runs as the project's owner rather than the caller.
        # Files and chunks carry the owner's tenant, so a member querying as
        # themselves would match nothing and get an empty answer that looks
        # exactly like a project with no documents.
        owner_user_id = project.owner_user_id
        # Trashed documents keep their vectors for TTL-bounded restore, so the
        # exclusion has to happen at query time. Combined with the caller's
        # per-question unchecked documents into one must_not list.
        records = (
            self.projects.list_files(
                owner_user_id=owner_user_id,
                project_id=project.project_id,
                include_trashed=True,
            )
            or []
        )
        trashed = [
            record.doc_id
            for record in records
            if record.trashed_at is not None and record.doc_id
        ]
        excluded_doc_ids = sorted({*trashed, *request.disabled_doc_ids})
        # Lets the chain drop the doc-scoped pass when routing already reaches
        # every active document -- same rows as the trash lookup, no extra query.
        active_doc_ids = {
            record.doc_id
            for record in records
            if record.doc_id
            and record.trashed_at is None
            and record.status is FileProcessingStatus.COMPLETED
        }
        cache_key = {
            "owner_user_id": owner_user_id,
            "project_id": project.project_id,
            "query": request.query,
            "rewritten_query": rewritten_query,
            "excluded_doc_ids": excluded_doc_ids,
            "profile": bundle.profile,
            "config": self.retrieval,
            "limit": request.limit,
            "language": request.language,
            # The class name is the fallback for rerankers that carry no model
            # attribute (the Protocol does not require one); it still separates
            # one implementation's entries from another's.
            "reranker": str(
                getattr(bundle.reranker, "model", type(bundle.reranker).__name__)
            ),
        }
        if self.retrieval_cache is not None:
            hit = self.retrieval_cache.get(**cache_key)
            if hit is not None:
                return hit
        index = QdrantHybridIndex(self.qdrant, bundle.collection_name, bundle.profile)
        if not self.qdrant.collection_exists(index.collection_name):
            # Nothing has been indexed for this embedding profile yet. That is an
            # empty result, not a fault: a project with no documents should fall
            # back to plain chat, which `build_turns` already does when the
            # evidence list is empty. Checked here rather than let the search run,
            # because `hybrid_search` calls `ensure_collection` and a read must
            # not create a collection as a side effect.
            return RetrievalOutcome()
        try:
            outcome = run_retrieval(
                index,
                bundle.embedder,
                bundle.reranker,
                request.query,
                owner_user_id=owner_user_id,
                project_id=project.project_id,
                language=request.language,
                limit=request.limit,
                config=self.retrieval,
                rewritten_query=rewritten_query,
                exclude_doc_ids=excluded_doc_ids,
                project_doc_ids=active_doc_ids,
            )
        except ProviderError as error:
            raise HTTPException(
                status_code=503, detail="Provider request failed"
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=503, detail="Query configuration error"
            ) from error
        if self.retrieval_cache is not None:
            self.retrieval_cache.set(outcome, **cache_key)
        return outcome

    # --- conversation window ----------------------------------------------------

    def conversation_window(
        self, owner_user_id: str, thread_id: str
    ) -> list[ThreadMessage] | None:
        """What the turn is answered against: the newest slice plus the opener.

        `list_messages` caps at its limit and gives up the beginning of a long
        thread, which is right for recency and wrong for the opening turn, the
        one message `window` always replays. So it is fetched back by position
        when the slice no longer reaches it.
        """
        recent = self.thread_store().list_messages(
            owner_user_id=owner_user_id, thread_id=thread_id
        )
        if recent is None:
            return None
        if recent and recent[0].position > 1:
            opener = (
                self.thread_store().list_messages_range(
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    from_position=1,
                    through_position=1,
                )
                or []
            )
            return [*opener, *recent]
        return recent

    def chars_per_token(self, thread: Thread, model_id: str) -> float:
        """Chars-to-tokens calibration, from the thread's own last turn.

        Measured under the same model, with the same language, on the same
        conversation -- far closer than any fixed constant. A ratio recorded
        under a *different* model is discarded: tokenizers differ enough
        (Sonnet 5 counts ~30% more tokens than 4.6 for the same text) that
        reusing it would mis-size the window. Clamped so one corrupt row
        cannot blow the estimate up or down without bound.
        """
        if (
            thread.last_model_id == model_id
            and thread.last_prompt_tokens
            and thread.last_prompt_chars
        ):
            measured = thread.last_prompt_chars / thread.last_prompt_tokens
            return min(8.0, max(1.0, measured))
        return DEFAULT_CHARS_PER_TOKEN

    def preflight_turns(
        self,
        *,
        owner_user_id: str,
        thread: Thread,
        prior: list[ThreadMessage],
        spec: ModelSpec,
        rebuild: Callable[[list[ChatTurn], str | None], list[ChatTurn]],
    ) -> tuple[list[ChatTurn], Thread]:
        """Size the history window to the model and fold what falls out --
        before generation, so the prompt that goes out never silently misses
        turns that nothing carries.

        `rebuild(history, summary)` assembles the full prompt for a candidate
        history; called with an empty history it measures everything that is
        not history (system sections, evidence, question), which is what the
        window's char budget must leave room for.

        On overflow the window is cut to the refill share of its caps and the
        newly-dropped span is folded in one summarizer call. The refill is the
        amortization: without it, a thread past its cap would pay a fold on
        every single turn.
        """
        cpt = self.chars_per_token(thread, spec.id)
        budget_chars = int(usable_tokens(spec) * cpt)
        reserved = sum(len(turn.content) for turn in rebuild([], thread.summary))
        floor = self.conversation.history_keep_recent

        kept, boundary = plan_history(
            prior,
            floor=floor,
            cap=spec.keep_recent_max_messages,
            budget_chars=budget_chars - reserved,
        )
        watermark = thread.summarized_through_position or 0
        if boundary > watermark and self.summarizer is not None:
            # Overflow: cut deeper than strictly needed so the freed headroom
            # absorbs the next several turns, then fold up to the new edge.
            refill_cap = max(floor, int(spec.keep_recent_max_messages * HISTORY_REFILL_RATIO))
            kept, boundary = plan_history(
                prior,
                floor=floor,
                cap=refill_cap,
                budget_chars=int((budget_chars - reserved) * HISTORY_REFILL_RATIO),
            )
            folded = self.fold_through(
                owner_user_id, thread, through_position=boundary, spec=spec
            )
            if folded is not None:
                thread = folded
                # The summary just grew, so the reserve it lives in did too.
                reserved = sum(
                    len(turn.content) for turn in rebuild([], thread.summary)
                )
                kept, _ = plan_history(
                    prior,
                    floor=floor,
                    cap=refill_cap,
                    budget_chars=int((budget_chars - reserved) * HISTORY_REFILL_RATIO),
                )
        history = [_as_turn(message) for message in kept]
        return rebuild(history, thread.summary), thread

    def compact_thread(
        self,
        owner_user_id: str,
        thread: Thread,
        *,
        instructions: str | None = None,
    ) -> tuple[Thread, bool, bool]:
        """The gauge's click-to-compact: fold everything outside the floor
        window, now, without waiting for the budget to force it.

        Works the backlog off in bounded folds (each capped by
        `summary_max_fold_messages` and `summary_max_fold_chars`) with a hard
        iteration ceiling, so one click on a very long thread does a lot of
        good and never runs away. Returns the (possibly updated) thread,
        whether anything changed, and whether backlog was left behind --
        a failed fold and a hit ceiling both look like a silent no-op to the
        client otherwise, and "nothing to compact" would be a lie.
        """
        prior = (
            self.conversation_window(owner_user_id, thread.thread_id) or []
        )
        kept, boundary = plan_history(
            prior,
            floor=self.conversation.history_keep_recent,
            cap=self.conversation.history_keep_recent,
            budget_chars=0,  # floor wins over an empty budget by design
        )
        changed = False
        for _ in range(10):
            if boundary <= (thread.summarized_through_position or 0):
                break
            updated = self.fold_through(
                owner_user_id,
                thread,
                through_position=boundary,
                instructions=instructions,
            )
            if updated is None:
                break
            thread = updated
            changed = True
        stalled = boundary > (thread.summarized_through_position or 0)
        if changed:
            estimated = self.estimate_prompt_after_compact(
                owner_user_id, thread, kept
            )
            if estimated is not None:
                thread = estimated
        return thread, changed, stalled

    def estimate_prompt_after_compact(
        self, owner_user_id: str, thread: Thread, kept: list[ThreadMessage]
    ) -> Thread | None:
        """Move the gauge the compact just earned, without waiting for the
        next measured turn.

        The estimate is the persistent part of the next prompt -- the floor
        window plus the new summary -- priced at the thread's own measured
        chars/token. Evidence and system sections are left out because they
        belong to the next question, not to this thread's state; the next
        real turn replaces the estimate with a measurement anyway. Storing
        chars alongside tokens at the same ratio keeps the calibration the
        estimate was priced with.

        None (thread untouched) when there is no measured turn to calibrate
        against -- the gauge was hidden then, and an uncalibrated guess
        should not unhide it.
        """
        if not thread.last_model_id or not thread.last_prompt_tokens:
            return None
        candidate_chars = sum(len(message.content) for message in kept) + len(
            thread.summary or ""
        )
        tokens = max(
            1,
            round(
                candidate_chars
                / self.chars_per_token(thread, thread.last_model_id)
            ),
        )
        return self.thread_store().record_prompt_metrics(
            owner_user_id=owner_user_id,
            thread_id=thread.thread_id,
            prompt_tokens=tokens,
            prompt_chars=candidate_chars,
            model_id=thread.last_model_id,
        )

    def record_summary_use(
        self, owner_user_id: str, thread: Thread, used_ids: list[str] | None
    ) -> None:
        """Credit the summary items the answering model said it leaned on.

        `None` means the model omitted the marker: no measurement, so nothing
        moves. An empty list is a real measurement -- the answer leaned on
        nothing -- and also moves nothing, but for a different reason.

        Ids the summary does not hold are dropped: a hallucinated id must not
        credit some other item by accident.
        """
        if not used_ids:
            if used_ids is None:
                _log.info(
                    "No used-marker on the answer for thread=%s; nothing credited",
                    thread.thread_id,
                )
            return
        items = parse_items(thread.summary)
        if not items:
            return
        known = {item.id for item in items}
        credited = [item_id for item_id in used_ids if item_id in known]
        if len(credited) != len(used_ids):
            _log.info(
                "Used-marker named unknown item(s) %s for thread=%s; ignored",
                sorted(set(used_ids) - known),
                thread.thread_id,
            )
        if not credited:
            return
        updated = [
            item.more_used() if item.id in credited else item for item in items
        ]
        self.thread_store().set_summary(
            owner_user_id=owner_user_id,
            thread_id=thread.thread_id,
            summary=render_items(updated),
            # Untouched: crediting an item says nothing about how far the
            # summary reaches, and moving the watermark here would skip turns.
            summarized_through_position=thread.summarized_through_position or 0,
        )

    def fold_through(
        self,
        owner_user_id: str,
        thread: Thread,
        *,
        through_position: int,
        instructions: str | None = None,
        spec: ModelSpec | None = None,
    ) -> Thread | None:
        """Fold the span between the watermark and `through_position` into
        the summary. Returns the updated thread, or None when nothing folded.

        Incremental: the previous summary plus only the new span, so one fold
        costs the same on turn 100 as on turn 10. The fetch is capped by
        `summary_max_fold_messages` and the fold by `summary_max_fold_chars`
        -- the count bounds the query, the chars bound what one summarizer
        call is handed, because 40 RAG-sized answers and 40 short turns are
        nothing alike. A long backlog is worked off across calls; the
        watermark records what was *actually* folded.

        Every failure path returns None and the turn proceeds: a thread that
        cannot be summarized must still be answerable.
        """
        if self.summarizer is None:
            return None
        # The fold runs on the session's model: the turn's spec when a turn
        # is in flight, else whatever the thread last answered on. Its quota
        # and its quirks stay one model's instead of a separate env pick.
        if spec is None:
            spec = self.models.resolve(thread.last_model_id)
        watermark = thread.summarized_through_position or 0
        if through_position <= watermark:
            return None
        # From position 2 at the earliest: the opening turn is replayed
        # verbatim forever, so it never belongs to the summary.
        fresh = (
            self.thread_store().list_messages_range(
                owner_user_id=owner_user_id,
                thread_id=thread.thread_id,
                from_position=max(watermark, 1) + 1,
                through_position=through_position,
                limit=self.conversation.summary_max_fold_messages,
            )
            or []
        )
        if not fresh:
            return None
        budget = self.conversation.summary_max_fold_chars
        spent = 0
        trimmed: list[ThreadMessage] = []
        for message in fresh:
            # The first message folds unconditionally: one oversized message
            # must still advance the watermark or the backlog stalls forever.
            if trimmed and spent + len(message.content) > budget:
                break
            trimmed.append(message)
            spent += len(message.content)
        fresh = trimmed
        merged = self.summarizer.summarize(
            previous=thread.summary,
            turns=[_as_turn(item) for item in fresh],
            sticky_key=thread.thread_id,
            instructions=instructions,
            model=spec.id,
            router=spec.router,
        )
        if not merged or merged == thread.summary:
            return None
        updated = self.thread_store().set_summary(
            owner_user_id=owner_user_id,
            thread_id=thread.thread_id,
            summary=merged,
            summarized_through_position=fresh[-1].position,
        )
        if updated is None:
            _log.warning("Could not store the summary for thread=%s", thread.thread_id)
            return None
        _log.info(
            "Summarized thread=%s: folded %d message(s), watermark %d -> %d",
            thread.thread_id,
            len(fresh),
            watermark,
            fresh[-1].position,
        )
        return updated

    def understand_context(
        self, messages: list[ThreadMessage], summary: str | None = None
    ) -> str:
        """What UnderstandMessage sees besides the current message.

        Two parts, each with its own budget. The recent raw turns are what
        resolve a pronoun most of the time; the thread summary is the only
        thing that can resolve one pointing at something that has already
        scrolled out of that window, which is exactly what the small message
        count makes more likely.

        The budgets are separate on purpose: sharing one would let a long
        summary evict the recent turns, and those are the stronger signal.

        Not TurnScorer's cosine/z-score selection over embedded history (that
        node is not built) -- just the last few turns, trimmed the same way
        `history_turns` trims: newest first until the cap is hit, then
        restored to conversation order.
        """
        selected: list[str] = []
        budget = self.conversation.understand_context_chars
        for message in reversed(
            messages[-self.conversation.understand_context_messages :]
        ):
            line = f"{message.role.value}: {message.content}"
            if len(line) > budget:
                break
            budget -= len(line)
            selected.append(line)
        selected.reverse()

        sections: list[str] = []
        summary_budget = self.conversation.understand_summary_chars
        if summary and summary_budget:
            # Labelled, and labelled as lossy: this is generated prose, not
            # transcript, so the model must not treat a detail in it as
            # something the user actually said. The design's §3.4 rule is that
            # a rewrite is never based on generated text; feeding the summary
            # here is a deliberate exception, and saying so in the text is what
            # keeps it from being read as a quote.
            sections.append(
                "Earlier in this conversation (summary, may be lossy):\n"
                + summary[:summary_budget]
            )
        if selected:
            sections.append("Recent turns:\n" + "\n".join(selected))
        return "\n\n".join(sections)

    # --- memory -----------------------------------------------------------------

    def load_memory(self, owner_user_id: str, thread: Thread) -> list[Memory]:
        """Memory for one turn: thread scope, project scope, and user scope.

        One query. Returns nothing rather than raising when memory is not
        configured or the owner has switched it off, because chat must keep
        working either way; `memory_store()` raising 503 is right for the CRUD
        routes and wrong here.
        """
        if self.memory is None:
            _log.debug("Memory store not configured; prompt gets no memory")
            return []
        try:
            if not self.memory.get_settings(owner_user_id=owner_user_id).memory_enabled:
                _log.debug("Memory disabled for this user; prompt gets no memory")
                return []
            recalled = self.memory.recall(
                owner_user_id=owner_user_id,
                thread_id=thread.thread_id,
                project_id=thread.project_id,
            )
        except Exception:
            # The docstring's "chat must keep working either way" extends to the
            # store being down, not just switched off: this runs before the user
            # message is even saved, so an escaped error here fails the whole
            # turn for a prompt garnish. An answer without memory beats no
            # answer.
            _log.exception(
                "Memory recall failed for thread=%s; answering without memory",
                thread.thread_id,
            )
            return []
        # Values in full, and deliberately: the rendered system prompt is logged
        # too, so having both is what shows which memories were recalled and then
        # dropped between here and the prompt, by scope resolution or by the
        # character budget. Keys alone cannot show that.
        _log.debug(
            "Recalled %d memories for thread=%s project=%s:%s",
            len(recalled),
            thread.thread_id,
            thread.project_id,
            "".join(
                f"\n  [{item.scope.value}] {item.type.value} {item.key} = {item.value}"
                for item in recalled
            )
            or " none",
        )
        return recalled

    def write_or_propose(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        source_message_id: str,
        candidate: MemoryCandidate,
        scope: MemoryScope,
        project_id: str | None = None,
    ) -> bool:
        """Write one candidate, falling back to a proposal when the cap blocks it.

        Returns whether the memory itself was written. The fallback matters
        because extraction is automatic: a pipeline the user never confirms must
        not lose writes in silence, so a full scope turns into something they can
        see and act on rather than a dropped candidate.
        """
        assert self.memory is not None  # guarded by the caller
        try:
            self.memory.add_memory(
                owner_user_id=owner_user_id,
                scope=scope,
                type=candidate.type,
                key=candidate.key,
                value=candidate.value,
                source=MemorySource.LLM_EXTRACTED,
                thread_id=thread_id if scope is MemoryScope.THREAD else None,
                project_id=project_id if scope is MemoryScope.PROJECT else None,
                source_message_id=source_message_id,
            )
            return True
        except ValueError:
            # Cap reached and this key is not already live, so there is nothing
            # to supersede.
            _log.warning(
                "Memory cap reached for scope=%s key=%s; proposing instead",
                scope.value,
                candidate.key,
            )
            self.memory.add_proposal(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                suggested_scope=scope,
                type=candidate.type,
                key=candidate.key,
                value=candidate.value,
                source_message_id=source_message_id,
            )
            return False

    def memory_tools(
        self,
        *,
        owner_user_id: str,
        thread: Thread,
        source_message_id: str,
        recalled: Sequence[Memory],
    ) -> list[object]:
        """The turn's memory tools, bound to this request's tenant.

        The tools themselves live in `rag_ingestion.memory.tools`; all this
        layer supplies is a writer that knows the owner, the thread and the
        cap/proposal policy.
        """
        if self.memory is None:
            return []
        return build_memory_tools(
            _ContextMemoryWriter(
                context=self,
                owner_user_id=owner_user_id,
                thread=thread,
                source_message_id=source_message_id,
                recalled=recalled,
            ),
            project_id=thread.project_id,
        )


@dataclass(frozen=True)
class _ContextMemoryWriter:
    """`MemoryWriter` for one turn: the tenant plus the store's write policy.

    Exists so `memory/tools.py` needs nothing from the API layer. Dedup reads
    the recall already loaded for this turn rather than querying again, which
    is what makes a user repeating their name cost no write at all.
    """

    context: ApiContext
    owner_user_id: str
    thread: Thread
    source_message_id: str
    recalled: Sequence[Memory]

    def is_enabled(self) -> bool:
        assert self.context.memory is not None
        return self.context.memory.get_settings(
            owner_user_id=self.owner_user_id
        ).memory_enabled

    def already_saved(self, *, key: str, value: str) -> bool:
        # Normalized, not compared raw: "Keep it short" restated as
        # "keep  it short " is the same instruction, and treating it as new
        # supersedes a live row to record that nothing changed.
        incoming = normalize(value)
        return any(
            item.key == key and normalize(item.value) == incoming
            for item in self.recalled
        )

    def save(
        self, *, scope: MemoryScope, key: str, value: str, type: MemoryType
    ) -> bool:
        candidate = MemoryCandidate(type=type, key=key, value=value)
        written = self.context.write_or_propose(
            owner_user_id=self.owner_user_id,
            thread_id=self.thread.thread_id,
            source_message_id=self.source_message_id,
            candidate=candidate,
            scope=scope,
            project_id=self.thread.project_id,
        )
        if written and scope is MemoryScope.THREAD:
            self._promote_if_habitual(candidate)
        return written

    def _promote_if_habitual(self, candidate: MemoryCandidate) -> None:
        """A conversation-scoped key seen in enough separate threads is a habit.

        Only THREAD scope is eligible: a project memory is about the project
        however often it recurs, and a user memory is already global.
        Promotion adds a user-scoped row rather than moving this one, so each
        thread keeps the memory it earned and `add_memory` supersedes any
        older global value for the same key.
        """
        assert self.context.memory is not None
        seen_in = self.context.memory.distinct_thread_count(
            owner_user_id=self.owner_user_id, key=candidate.key
        )
        if not should_promote(
            distinct_threads=seen_in,
            threshold=self.context.conversation.promotion_thread_threshold,
        ):
            return
        _log.info(
            "Memory key=%s seen in %d threads; promoting to user scope",
            candidate.key,
            seen_in,
        )
        self.context.write_or_propose(
            owner_user_id=self.owner_user_id,
            thread_id=self.thread.thread_id,
            source_message_id=self.source_message_id,
            candidate=candidate,
            scope=MemoryScope.USER,
        )
