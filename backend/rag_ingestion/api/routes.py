"""The app factory: resolves defaults, builds the shared `ApiContext`, and
mounts one router per domain.

The map, so a new reader does not have to open every file:
  schemas.py          -- request/response models
  deps.py             -- `ApiContext` (stores, providers, configs) and helpers
  routers/system.py   -- /health, /settings, /usage
  routers/projects.py -- /projects (CRUD, documents, media, uploads, query)
  routers/threads.py  -- /threads (CRUD, streaming chat)
  routers/memory.py   -- /memory (proposals, CRUD)
"""

from __future__ import annotations

import time
from concurrent.futures import Executor, ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import QdrantClient

from rag_ingestion.chat import ChatCompleter
from rag_ingestion.config import ChunkingConfig, ConversationConfig, UploadConfig
from rag_ingestion.retrieval import RetrievalConfig
from rag_ingestion.retrieval.answering import Answerer, MockAnswerer
from rag_ingestion.retrieval.cache import RetrievalCache
from rag_ingestion.model_catalog import load_catalog
from rag_ingestion.schemas.model_catalog import ModelCatalog
from rag_ingestion.summary import Summarizer
from rag_ingestion.understanding import Understander
from rag_ingestion.ingestion.parsers.datalab import DocumentConverter
from rag_ingestion.ingestion.parsers.pdf import ScanParser
from rag_ingestion.storage.media import MediaStore
from rag_ingestion.memory.models import MemoryStore
from rag_ingestion.storage.projects import ProjectStore
from rag_ingestion.storage.threads import ThreadStore
from rag_ingestion.storage.admin import AdminStore
from rag_ingestion.storage.threads import normalize_email
from rag_ingestion.storage.users import UserStore
from rag_ingestion.providers.registry import ProviderRegistry
from rag_ingestion.api.auth import (
    FirebaseTokenError,
    FirebaseTokenVerifier,
    is_anonymous_id,
    reset_authenticated_email,
    reset_authenticated_user_id,
    set_authenticated_email,
    set_authenticated_user_id,
)
from rag_ingestion.api.deps import ApiContext
from rag_ingestion.api.routers import account as account_router
from rag_ingestion.api.routers import admin as admin_router
from rag_ingestion.api.routers import memory as memory_router
from rag_ingestion.api.routers import projects as projects_router
from rag_ingestion.api.routers import reports as reports_router
from rag_ingestion.api.routers import share as share_router
from rag_ingestion.api.routers import system as system_router
from rag_ingestion.api.routers import threads as threads_router


def create_query_app(
    qdrant: QdrantClient,
    registry: ProviderRegistry,
    projects: ProjectStore,
    *,
    answerer: Answerer | None = None,
    conversation: ConversationConfig | None = None,
    chunking: ChunkingConfig | None = None,
    upload: UploadConfig | None = None,
    upload_executor: Executor | None = None,
    media_store: MediaStore | None = None,
    scan_parser: ScanParser | None = None,
    converter: DocumentConverter | None = None,
    retrieval_config: RetrievalConfig | None = None,
    retrieval_cache: RetrievalCache | None = None,
    threads: ThreadStore | None = None,
    chat: ChatCompleter | None = None,
    memory: MemoryStore | None = None,
    understand: Understander | None = None,
    summarizer: Summarizer | None = None,
    auth_verifier: FirebaseTokenVerifier | None = None,
    allow_x_user_id_fallback: bool = True,
    users: UserStore | None = None,
    admins: AdminStore | None = None,
    admin_emails: tuple[str, ...] | list[str] = (),
    models: ModelCatalog | None = None,
    configured_routers: frozenset[str] | None = None,
) -> FastAPI:
    # The positive-value checks that used to sit here are gone: every one of
    # these numbers now carries `gt=0` on its config field, so an invalid value
    # is rejected where it is set instead of one layer later.
    active_upload = upload or UploadConfig()
    owns_upload_executor = upload_executor is None
    active_upload_executor = upload_executor or ThreadPoolExecutor(
        max_workers=active_upload.worker_count,
        thread_name_prefix="rag-upload",
    )
    app = FastAPI(title="RAG API", version="2.0.0")
    app.state.upload_executor = active_upload_executor
    if owns_upload_executor:
        app.router.add_event_handler("shutdown", active_upload_executor.shutdown)

    ctx = ApiContext(
        qdrant=qdrant,
        registry=registry,
        projects=projects,
        answerer=answerer or MockAnswerer(),
        conversation=conversation or ConversationConfig(),
        chunking=chunking or ChunkingConfig(),
        upload=active_upload,
        upload_executor=active_upload_executor,
        media_store=media_store,
        scan_parser=scan_parser,
        converter=converter,
        retrieval=retrieval_config or RetrievalConfig(),
        retrieval_cache=retrieval_cache,
        threads=threads,
        chat=chat,
        memory=memory,
        understand=understand,
        summarizer=summarizer,
        users=users,
        admins=admins,
        admin_emails=tuple(normalize_email(email) for email in admin_emails),
        models=models or load_catalog(),
        routers=configured_routers,
    )

    # One registry write per uid per window, not per request.
    login_seen_at: dict[str, float] = {}
    login_record_window_seconds = 900.0

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        token = set_authenticated_user_id(None)
        email_token = set_authenticated_email(None)
        try:
            # The one public surface: a share link may be read by people who
            # have no account, so a missing token must not 401 here even when
            # the fallback is off. A token that *is* present still has to be
            # verified, because an invite share decides access from it.
            optional_auth = request.url.path.startswith(share_router.PUBLIC_PREFIX)
            if auth_verifier is None:
                return await call_next(request)

            authorization = request.headers.get("Authorization", "").strip()
            if not authorization and optional_auth:
                return await call_next(request)
            if not authorization:
                if not allow_x_user_id_fallback:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Missing Authorization header"},
                    )
                # The fallback only carries anonymous identities. Any other
                # shape is an attempt to act as a real account without its
                # token, so it is refused rather than passed through.
                header_id = request.headers.get("X-User-ID", "").strip()
                if header_id and not is_anonymous_id(header_id):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "This identity requires a login token"},
                    )
                return await call_next(request)
            try:
                identity = auth_verifier.verify_bearer(authorization)
            except FirebaseTokenError as error:
                return JSONResponse(
                    status_code=401,
                    content={"detail": str(error)},
                )
            # A login token must never resolve into the anonymous namespace,
            # or the two identity kinds would stop being distinguishable.
            if is_anonymous_id(identity.uid):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Login token carries an anonymous-shaped uid"},
                )

            if users is not None:
                now = time.monotonic()
                last = login_seen_at.get(identity.uid)
                if last is None or now - last > login_record_window_seconds:
                    users.record_login(
                        uid=identity.uid,
                        email=identity.email,
                        display_name=identity.display_name,
                    )
                    login_seen_at[identity.uid] = now

            # Live on every request, never cached like the login write above:
            # disabling an account has to take effect now, not within some
            # window. 403 rather than 401 -- the token is valid, the account
            # is refused.
            if users is not None and users.is_disabled(identity.uid):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "This account has been disabled"},
                )

            reset_authenticated_user_id(token)
            token = set_authenticated_user_id(identity.uid)
            # Only a verified address is published. An unverified one is a
            # claim, not a fact, and matching it against an invitation would
            # let anyone read a share by signing up with the invited address.
            if identity.email and identity.email_verified:
                reset_authenticated_email(email_token)
                email_token = set_authenticated_email(identity.email)
            return await call_next(request)
        finally:
            reset_authenticated_user_id(token)
            reset_authenticated_email(email_token)

    system_router.register(app, ctx)
    account_router.register(app, ctx)
    projects_router.register(app, ctx)
    threads_router.register(app, ctx)
    share_router.register(app, ctx)
    reports_router.register(app, ctx)
    memory_router.register(app, ctx)
    admin_router.register(app, ctx)
    return app
