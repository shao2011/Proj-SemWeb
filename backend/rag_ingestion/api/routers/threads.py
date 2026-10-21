"""Thread routes: CRUD plus the streaming chat turn.

`create_thread_message` is the heart of the app: it saves the user message,
optionally retrieves evidence (project threads run a rewrite-only understand
first, because retrieval needs it), sizes the history window to the picked
model and folds overflow into the summary pre-flight, then streams the answer
as SSE with memory tools the model can call inline. `done` carries the
server-computed context gauge.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from rag_ingestion.logger import get_logger
from rag_ingestion.model_catalog import context_percent
from rag_ingestion.retrieval.answering import build_turns
from rag_ingestion.schemas.answering import AnswerEvidence
from rag_ingestion.schemas.understanding import Understanding
from rag_ingestion.storage.threads import (
    Citation,
    Thread,
    ThreadMessageRole,
    ThreadShare,
)
from rag_ingestion.summary import UsedMarkerFilter
from rag_ingestion.providers.registry import ProviderError
from rag_ingestion.api.auth import authenticated_email, is_anonymous_id
from rag_ingestion.api.deps import (
    ApiContext,
    context_evidence,
    sse,
    to_response,
    user_id,
)
from rag_ingestion.api.schemas import (
    CompactThreadRequest,
    CompactThreadResponse,
    CreateShareRequest,
    CreateThreadRequest,
    ForkThreadRequest,
    QueryRequest,
    RenameThreadRequest,
    SharedWithMeItem,
    ShareRecipientRequest,
    ShareView,
    ThreadDetail,
    ThreadMessageRequest,
    ThreadView,
)

_log = get_logger(__name__)


class _CompactingSet:
    """Thread ids with a compaction in flight, add-if-absent atomically."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def add(self, thread_id: str) -> bool:
        with self._lock:
            if thread_id in self._active:
                return False
            self._active.add(thread_id)
            return True

    def discard(self, thread_id: str) -> None:
        with self._lock:
            self._active.discard(thread_id)


_compacting = _CompactingSet()


def register(app: FastAPI, ctx: ApiContext) -> None:

    def view(thread: Thread) -> ThreadView:
        """The storage thread plus the server-computed context gauge."""
        return ThreadView(
            **thread.model_dump(),
            context_used_percent=ctx.context_used_percent(thread),
        )

    @app.post("/threads", response_model=ThreadView, status_code=status.HTTP_201_CREATED)
    def create_thread(
        request: CreateThreadRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ThreadView:
        owner_user_id = user_id(x_user_id)
        store = ctx.thread_store()
        if request.project_id is not None:
            # A member may keep their own conversations inside a project that
            # is shared with them; the thread stays theirs.
            ctx.accessible_project(owner_user_id, request.project_id)
        return view(
            store.create_thread(
                owner_user_id=owner_user_id,
                title=request.title,
                project_id=request.project_id,
            )
        )

    @app.get("/threads", response_model=list[ThreadView])
    def list_threads(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        limit: int = 50,
    ) -> list[ThreadView]:
        return [
            view(thread)
            for thread in ctx.thread_store().list_threads(
                owner_user_id=user_id(x_user_id), limit=max(1, min(limit, 200))
            )
        ]

    @app.get("/threads/{thread_id}", response_model=ThreadDetail)
    def get_thread(
        thread_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        limit: int = 200,
    ) -> ThreadDetail:
        owner_user_id = user_id(x_user_id)
        thread = ctx.owned_thread(owner_user_id, thread_id)
        messages = ctx.thread_store().list_messages(
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            limit=max(1, min(limit, 500)),
        )
        if messages is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return ThreadDetail(**view(thread).model_dump(), messages=messages)

    @app.patch("/threads/{thread_id}", response_model=ThreadView)
    def rename_thread(
        thread_id: str,
        request: RenameThreadRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ThreadView:
        renamed = ctx.thread_store().rename_thread(
            owner_user_id=user_id(x_user_id),
            thread_id=thread_id,
            title=request.title,
        )
        if renamed is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return view(renamed)

    @app.post("/threads/{thread_id}/compact", response_model=CompactThreadResponse)
    def compact_thread(
        thread_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        request: CompactThreadRequest | None = None,
    ) -> CompactThreadResponse:
        owner_user_id = user_id(x_user_id)
        thread = ctx.owned_thread(owner_user_id, thread_id)
        if ctx.summarizer is None:
            raise HTTPException(
                status_code=503, detail="Summarizer is not configured"
            )
        # One compaction per thread at a time: a double-click must not run two
        # summarizer chains over the same span. In-process is enough for the
        # single-process deployment; the folds themselves are idempotent by
        # watermark, so a race across processes wastes a call, not data.
        if not _compacting.add(thread_id):
            raise HTTPException(status_code=409, detail="Already compacting")
        try:
            updated, changed, stalled = ctx.compact_thread(
                owner_user_id,
                thread,
                instructions=request.instructions if request else None,
            )
        finally:
            _compacting.discard(thread_id)
        return CompactThreadResponse(
            changed=changed,
            stalled=stalled,
            summarized_through_position=updated.summarized_through_position,
            context_used_percent=ctx.context_used_percent(updated),
        )

    @app.post(
        "/threads/{thread_id}/fork",
        response_model=ThreadView,
        status_code=status.HTTP_201_CREATED,
    )
    def fork_thread(
        thread_id: str,
        request: ForkThreadRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ThreadView:
        owner_user_id = user_id(x_user_id)
        # The anonymous allowance counts user messages across every thread the
        # identity owns, and a fork copies some. Letting it through would burn
        # a trial on messages the visitor never sent, so the refusal is the
        # honest answer.
        if is_anonymous_id(owner_user_id):
            raise HTTPException(status_code=403, detail="login_required_to_fork")
        thread = ctx.owned_thread(owner_user_id, thread_id)
        forked = ctx.thread_store().fork_thread(
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            through_position=request.through_position,
            title=f"{thread.title} (continued)"[:200],
        )
        if forked is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return view(forked)

    def share_view(share: ThreadShare, message_count: int) -> ShareView:
        return ShareView(
            share_id=share.share_id,
            shared_through_position=share.shared_through_position,
            message_count=message_count,
            access=share.access,
            recipients=share.recipients,
            created_at=share.created_at,
            updated_at=share.updated_at,
        )

    def message_count_of(owner_user_id: str, thread_id: str) -> int:
        return (
            ctx.thread_store().count_messages(
                owner_user_id=owner_user_id, thread_id=thread_id
            )
            or 0
        )

    @app.get("/shared", response_model=list[SharedWithMeItem])
    def list_shared_with_me() -> list[SharedWithMeItem]:
        """Chats other people invited this address to.

        Keyed on the verified address rather than the uid, because that is
        what an owner types when inviting somebody who may not have signed up
        yet. No login, or a login whose address the provider never verified,
        means there is nothing this caller can be shown.
        """
        email = authenticated_email()
        if email is None:
            raise HTTPException(status_code=401, detail="login_required_for_share")
        return [
            SharedWithMeItem(
                share_id=share.share_id,
                title=share.title,
                updated_at=share.updated_at,
            )
            for share in ctx.thread_store().list_shares_for_email(email=email)
        ]

    @app.post(
        "/threads/{thread_id}/share/recipients",
        response_model=ShareView,
        status_code=status.HTTP_201_CREATED,
    )
    def add_share_recipient(
        thread_id: str,
        request: ShareRecipientRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ShareView:
        owner_user_id = user_id(x_user_id)
        ctx.owned_thread(owner_user_id, thread_id)
        updated = ctx.thread_store().add_share_recipient(
            owner_user_id=owner_user_id, thread_id=thread_id, email=str(request.email)
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="This chat is not shared")
        return share_view(updated, message_count_of(owner_user_id, thread_id))

    @app.delete(
        "/threads/{thread_id}/share/recipients",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def remove_share_recipient(
        thread_id: str,
        request: ShareRecipientRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        owner_user_id = user_id(x_user_id)
        ctx.owned_thread(owner_user_id, thread_id)
        updated = ctx.thread_store().remove_share_recipient(
            owner_user_id=owner_user_id, thread_id=thread_id, email=str(request.email)
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="This chat is not shared")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/threads/{thread_id}/share",
        response_model=ShareView,
        status_code=status.HTTP_201_CREATED,
    )
    def share_thread(
        thread_id: str,
        request: CreateShareRequest,
        response: Response,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ShareView:
        owner_user_id = user_id(x_user_id)
        # Checked before ownership: an anonymous visitor must be told to log
        # in, not that the thread does not exist. Enforced here rather than in
        # the UI, so the rule holds for any caller.
        if is_anonymous_id(owner_user_id):
            raise HTTPException(status_code=403, detail="login_required_to_share")
        thread = ctx.owned_thread(owner_user_id, thread_id)
        store = ctx.thread_store()
        message_count = (
            store.count_messages(owner_user_id=owner_user_id, thread_id=thread_id) or 0
        )
        existing = store.get_thread_share(
            owner_user_id=owner_user_id, thread_id=thread_id
        )
        if existing is not None:
            if request.refresh:
                existing = (
                    store.refresh_share(
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        title=thread.title,
                        through_position=message_count,
                    )
                    or existing
                )
            if request.access is not None and request.access is not existing.access:
                existing = (
                    store.set_share_access(
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                        access=request.access,
                    )
                    or existing
                )
            # Already shared: the same link back, not a second one.
            response.status_code = status.HTTP_200_OK
            return share_view(existing, message_count)
        return share_view(
            store.share_thread(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                title=thread.title,
                through_position=message_count,
            ),
            message_count,
        )

    @app.get("/threads/{thread_id}/share", response_model=ShareView)
    def get_thread_share(
        thread_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ShareView:
        owner_user_id = user_id(x_user_id)
        ctx.owned_thread(owner_user_id, thread_id)
        store = ctx.thread_store()
        share = store.get_thread_share(
            owner_user_id=owner_user_id, thread_id=thread_id
        )
        if share is None:
            raise HTTPException(status_code=404, detail="This chat is not shared")
        return share_view(
            share,
            store.count_messages(owner_user_id=owner_user_id, thread_id=thread_id) or 0,
        )

    @app.delete(
        "/threads/{thread_id}/share", status_code=status.HTTP_204_NO_CONTENT
    )
    def revoke_thread_share(
        thread_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        owner_user_id = user_id(x_user_id)
        if not ctx.thread_store().revoke_share(
            owner_user_id=owner_user_id, thread_id=thread_id
        ):
            raise HTTPException(status_code=404, detail="This chat is not shared")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_thread(
        thread_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        deleted = ctx.thread_store().delete_thread(
            owner_user_id=user_id(x_user_id), thread_id=thread_id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Thread not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/threads/{thread_id}/messages")
    def create_thread_message(
        thread_id: str,
        request: ThreadMessageRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> StreamingResponse:
        owner_user_id = user_id(x_user_id)
        store = ctx.thread_store()
        completer = ctx.chat_provider()
        thread = ctx.owned_thread(owner_user_id, thread_id)

        # The picked model, or the catalog default. The catalog is the source
        # of truth for every turn now that models live on different routers:
        # the env-configured provider model has no router of its own, so
        # "no pick" lands on the catalog default rather than on it. Budgets
        # (window, output cap, gauge) come from the same resolved spec.
        spec = ctx.models.resolve(request.model_id)
        if request.model_id is not None and spec.id != request.model_id:
            _log.warning(
                "Requested model %r is not in the catalog; using %r",
                request.model_id,
                spec.id,
            )
        # Same degradation as an unknown id: a model whose router has no keys
        # (a stale picker cache, a key pulled from the environment) answers on
        # the default rather than failing the turn. Logged dev-side only.
        if ctx.routers is not None and spec.router not in ctx.routers:
            _log.warning(
                "Model %r needs router %r, which has no API keys; using %r",
                spec.id,
                spec.router,
                ctx.models.default,
            )
            spec = ctx.models.resolve(None)
        model_used = spec.id

        # The authoritative anonymous trial limit; the client's counter is
        # only a courtesy copy. Checked before the message is stored, so a
        # refused message costs nothing. Spans all the identity's threads.
        limit = ctx.conversation.anon_message_limit
        if (
            limit > 0
            and is_anonymous_id(owner_user_id)
            and store.count_user_messages(owner_user_id=owner_user_id) >= limit
        ):
            raise HTTPException(
                status_code=403, detail="anonymous_message_limit_reached"
            )

        prior = ctx.conversation_window(owner_user_id, thread_id) or []
        user_message = store.add_message(
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            role=ThreadMessageRole.USER,
            content=request.content,
        )
        if user_message is None:
            raise HTTPException(status_code=404, detail="Thread not found")

        recalled = ctx.load_memory(owner_user_id, thread)

        def run_understanding() -> Understanding:
            if ctx.understand is None:
                return Understanding(rewritten_query=None, memory=[])
            return ctx.understand.understand(
                request.content,
                ctx.understand_context(prior, thread.summary),
                sticky_key=thread_id,
                model=spec.id,
                router=spec.router,
            )

        # Understanding is rewrite-only and project-only now: the rewrite feeds
        # retrieval, which streams nothing until it finishes. Memory extraction
        # moved into the generation call itself -- the answering model saves
        # memory through tools, so plain chat pays no extra LLM call at all.
        understanding: Understanding | None = None
        if thread.project_id is not None:
            understanding = run_understanding()
            if understanding.rewritten_query:
                _log.info(
                    "UnderstandMessage rewrote thread=%s message=%r -> %r",
                    thread_id,
                    request.content,
                    understanding.rewritten_query,
                )

        def emit() -> Iterator[str]:
            yield sse("user_message", user_message.model_dump(mode="json"))
            try:
                evidence: list[AnswerEvidence] = []
                citations: list[Citation] = []
                if thread.project_id is not None:
                    project, _role = ctx.accessible_project(
                        owner_user_id, thread.project_id
                    )
                    query = QueryRequest(
                        query=request.content,
                        limit=request.retrieval_limit,
                        disabled_doc_ids=request.disabled_doc_ids,
                    )
                    outcome = ctx.retrieve(
                        query,
                        owner_user_id=owner_user_id,
                        project=project,
                        rewritten_query=(
                            understanding.rewritten_query
                            if understanding is not None
                            else None
                        ),
                    )
                    retrieved = to_response(query, outcome)
                    evidence = context_evidence(outcome)
                    # Stored with the target, not just the chunk id: reopening
                    # a thread then costs no round trip, and the panel keeps
                    # working when Qdrant does not.
                    citations = [
                        Citation(
                            chunk_id=hit.chunk_id,
                            doc_id=hit.doc_id,
                            block_ids=hit.source_block_ids,
                        )
                        for hit in retrieved.hits
                    ]
                    yield sse("sources", retrieved.model_dump(mode="json"))

                def rebuild(history, summary):
                    return build_turns(
                        request.content,
                        evidence,
                        history=history,
                        memory=recalled,
                        summary=summary,
                        memory_max_items=ctx.conversation.memory_max_items,
                        memory_char_budget=ctx.conversation.memory_char_budget,
                    )

                # Window sized to the picked model, and anything that fell out
                # folded into the summary *before* generation: the prompt that
                # goes out never silently misses turns that nothing carries.
                turns, active_thread = ctx.preflight_turns(
                    owner_user_id=owner_user_id,
                    thread=thread,
                    prior=prior,
                    spec=spec,
                    rebuild=rebuild,
                )
                _log.info(
                    "Generating for thread=%s model=%s mode=%s evidence=%d "
                    "history=%d memory=%d summary=%s system_chars=%d",
                    thread_id,
                    model_used,
                    "grounded" if evidence else "plain",
                    len(evidence),
                    len(turns) - 2,
                    len(recalled),
                    "yes" if active_thread.summary else "no",
                    len(turns[0].content),
                )
                # Everything pushed to the LLM, only when DEBUG is on. This is
                # what answers "what did the model actually see": the rendered
                # memory, rules and evidence, plus the model and the cache
                # state, exactly as the request goes out. `cache` is always
                # null today -- the OpenRouter payload never sets a cache
                # directive, so this makes that visible per turn instead of
                # something you have to infer.
                _log.debug(
                    "LLM request for thread=%s:\n%s",
                    thread_id,
                    json.dumps(
                        {
                            "model": model_used,
                            "cache": None,
                            "messages": [
                                {"role": turn.role, "content": turn.content}
                                for turn in turns
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )

                # Chars of the prompt as built. The tokens reported back may
                # cover later tool rounds too, which only skews the chars/token
                # calibration toward over-estimating -- the safe direction.
                prompt_chars = sum(len(turn.content) for turn in turns)
                measured: dict[str, int] = {}

                def record_llm_usage(usage: dict[str, object]) -> None:
                    # Bookkeeping only: a usage row must never break a stream
                    # that already carried the answer.
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    if prompt_tokens > 0:
                        # Zero would poison the chars/token calibration, so a
                        # provider that reports no prompt size records nothing.
                        measured["prompt_tokens"] = prompt_tokens
                        try:
                            store.record_prompt_metrics(
                                owner_user_id=owner_user_id,
                                thread_id=thread_id,
                                prompt_tokens=prompt_tokens,
                                prompt_chars=prompt_chars,
                                model_id=model_used,
                            )
                        except Exception:
                            _log.exception("Recording prompt metrics failed")
                    if ctx.memory is None:
                        return
                    try:
                        ctx.memory.add_llm_usage(
                            owner_user_id=owner_user_id,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=int(usage.get("completion_tokens") or 0),
                        )
                    except Exception:
                        _log.exception("Recording LLM usage failed")

                memory_tools = ctx.memory_tools(
                    owner_user_id=owner_user_id,
                    thread=thread,
                    source_message_id=user_message.message_id,
                    recalled=recalled,
                )
                # The model ends its reply with `<!--used:...-->` naming the
                # summary items it leaned on. The filter holds those bytes
                # back rather than emitting and retracting them: SSE has no
                # take-backs, and a turn stopped mid-marker then leaves
                # nothing dangling because nothing was ever sent.
                used_marker = UsedMarkerFilter()
                parts: list[str] = []
                for text in completer.stream(
                    turns,
                    max_tokens=spec.max_output_tokens,
                    sticky_key=thread_id,
                    on_usage=record_llm_usage,
                    tools=memory_tools or None,
                    model=spec.id,
                    router=spec.router,
                ):
                    visible = used_marker.feed(text)
                    if visible:
                        parts.append(visible)
                        yield sse("delta", {"text": visible})
                tail = used_marker.close()
                if tail:
                    parts.append(tail)
                    yield sse("delta", {"text": tail})

                generated = "".join(parts).strip()
                if not generated:
                    yield sse("error", {"detail": "Chat provider returned empty text"})
                    return
                # Only what the answer actually referenced counts as a
                # citation; retrieval's full hit list stays on the sources
                # event. Small talk that cites nothing gets an empty list.
                cited_indices = {
                    int(number) for number in re.findall(r"\[cite:(\d{1,2})\]", generated)
                }
                citations = [
                    citation
                    for position, citation in enumerate(citations, start=1)
                    if position in cited_indices
                ]
                assistant = store.add_message(
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    role=ThreadMessageRole.ASSISTANT,
                    content=generated,
                    citations=citations,
                )
                if assistant is None:
                    yield sse("error", {"detail": "Thread not found"})
                    return
                # The gauge rides `done`, computed server-side from the tokens
                # the provider just reported. None when no usage arrived.
                yield sse(
                    "done",
                    {
                        **assistant.model_dump(mode="json"),
                        "context_used_percent": context_percent(
                            measured.get("prompt_tokens"), spec
                        ),
                        "model_id": model_used,
                    },
                )
                # After `done` on purpose: bookkeeping for the next turn must
                # never sit between the user and their answer, and its own
                # failure must not turn an answered turn into an error.
                try:
                    ctx.record_summary_use(
                        owner_user_id, active_thread, used_marker.ids
                    )
                except Exception:
                    _log.exception("Recording summary use failed")
            except HTTPException as error:
                yield sse("error", {"detail": str(error.detail)})
            except ProviderError:
                yield sse("error", {"detail": "Chat provider request failed"})

        return StreamingResponse(emit(), media_type="text/event-stream")

