from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from redis import Redis
from sqlalchemy import Engine
from qdrant_client import QdrantClient

from rag_ingestion.config import RuntimeConfig
from rag_ingestion.logger import configure_logging, get_logger
from rag_ingestion.storage.r2_media import build_media_store
from rag_ingestion.ingestion.parsers.datalab import DatalabClient
from rag_ingestion.db.base import target_metadata
from rag_ingestion.db.session import build_engine
from rag_ingestion.memory.models import MemoryScope
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_admin import PostgresAdminStore
from rag_ingestion.storage.pg_users import PostgresUserStore
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.core.agent import BaseAgent
from rag_ingestion.retrieval.answering import LLMAnswerer
from rag_ingestion.model_catalog import load_catalog
from rag_ingestion.summary import LLMSummarizer
from rag_ingestion.understanding import LLMUnderstander
from rag_ingestion.providers.adapters import (
    CohereEmbedder,
    CohereReranker,
    JinaEmbedder,
    JinaReranker,
    VoyageEmbedder,
    VoyageReranker,
)
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.providers.embedding_cache import RedisCachedEmbedder
from rag_ingestion.retrieval.cache import RetrievalCache
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.api.auth import FirebaseTokenVerifier
from rag_ingestion.api.routes import create_query_app

_log = get_logger(__name__)


def _profile(
    provider: Provider,
    model: str,
    version: str,
    dimensions: int,
) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider=provider.value,
        model=model,
        version=version,
        dimensions=dimensions,
        task="retrieval",
        mode="naive",
    )


def _bundles(
    config: RuntimeConfig,
    redis_client: Redis | None = None,
) -> list[ProviderBundle]:
    def cached(embedder):
        if redis_client is None:
            return embedder
        return RedisCachedEmbedder(
            embedder,
            redis_client,
            ttl_seconds=config.providers.embedding_cache_ttl_seconds,
        )

    bundles: list[ProviderBundle] = []
    if config.providers.cohere_api_keys:
        profile = _profile(
            Provider.COHERE,
            config.providers.cohere_embedding_model,
            config.providers.profile_version,
            config.providers.cohere_dimensions,
        )
        bundles.append(
            ProviderBundle(
                provider=Provider.COHERE,
                profile=profile,
                collection_name=f"{config.providers.collection_prefix}-cohere-{config.providers.profile_version}",
                embedder=cached(
                    CohereEmbedder(
                        config.providers.cohere_api_keys,
                        profile,
                        base_url=config.providers.cohere_base_url,
                        timeout=config.providers.request_timeout_seconds,
                    )
                ),
                reranker=CohereReranker(
                    config.providers.cohere_api_keys,
                    config.providers.cohere_rerank_model,
                    base_url=config.providers.cohere_base_url,
                    timeout=config.providers.request_timeout_seconds,
                ),
            )
        )
    if config.providers.voyage_api_keys:
        profile = _profile(
            Provider.VOYAGE,
            config.providers.voyage_embedding_model,
            config.providers.profile_version,
            config.providers.voyage_dimensions,
        )
        bundles.append(
            ProviderBundle(
                provider=Provider.VOYAGE,
                profile=profile,
                collection_name=f"{config.providers.collection_prefix}-voyage-{config.providers.profile_version}",
                embedder=cached(
                    VoyageEmbedder(
                        config.providers.voyage_api_keys,
                        profile,
                        base_url=config.providers.voyage_base_url,
                        timeout=config.providers.request_timeout_seconds,
                    )
                ),
                reranker=VoyageReranker(
                    config.providers.voyage_api_keys,
                    config.providers.voyage_rerank_model,
                    base_url=config.providers.voyage_base_url,
                    timeout=config.providers.request_timeout_seconds,
                ),
            )
        )
    if config.providers.jina_api_keys:
        profile = _profile(
            Provider.JINA,
            config.providers.jina_embedding_model,
            config.providers.profile_version,
            config.providers.jina_dimensions,
        )
        bundles.append(
            ProviderBundle(
                provider=Provider.JINA,
                profile=profile,
                collection_name=f"{config.providers.collection_prefix}-jina-{config.providers.profile_version}",
                embedder=cached(
                    JinaEmbedder(
                        config.providers.jina_api_keys,
                        profile,
                        base_url=config.providers.jina_base_url,
                        timeout=config.providers.request_timeout_seconds,
                    )
                ),
                reranker=JinaReranker(
                    config.providers.jina_api_keys,
                    config.providers.jina_rerank_model,
                    base_url=config.providers.jina_base_url,
                    timeout=config.providers.request_timeout_seconds,
                ),
            )
        )
    return bundles


def create_runtime_app(
    config: RuntimeConfig | None = None,
    *,
    qdrant_client: QdrantClient | None = None,
    engine: Engine | None = None,
    redis_client: Redis | None = None,
    create_tables: bool = True,
) -> FastAPI:
    runtime_config = config or RuntimeConfig.from_environment()
    configure_logging(
        level=runtime_config.logging.level,
        log_file=runtime_config.logging.file,
        log_sql=runtime_config.logging.sql,
    )
    # Ownership is tracked for every connection-holding resource, not only Redis:
    # a caller that passes one in owns its lifetime and closing it on shutdown
    # would pull it out from under them, and one this function opens has nobody
    # else to close it. Repeated app construction, which the test suite does
    # constantly, otherwise leaves engines and Qdrant clients holding sockets.
    owns_qdrant_client = qdrant_client is None
    client = qdrant_client or QdrantClient(
        url=runtime_config.qdrant_url, api_key=runtime_config.qdrant_api_key
    )
    owns_redis_client = redis_client is None and runtime_config.redis_url is not None
    cache = redis_client
    if cache is None and runtime_config.redis_url is not None:
        cache = Redis.from_url(
            runtime_config.redis_url,
            socket_connect_timeout=runtime_config.redis_connect_timeout_seconds,
            socket_timeout=runtime_config.redis_read_timeout_seconds,
        )
    owns_engine = engine is None
    db_engine = engine or build_engine(runtime_config.database_url)
    if create_tables:
        # Convenience for local development. Alembic owns the schema in any
        # environment where migrations are run.
        target_metadata.create_all(db_engine)
    # Opt-in: it only pays for itself where the same question recurs, and a
    # stale answer is worse than a slow one, so it stays off unless asked for.
    retrieval_cache = (
        RetrievalCache(cache, ttl_seconds=runtime_config.retrieval_cache_ttl_seconds)
        if cache is not None and runtime_config.retrieval_cache_enabled
        else None
    )
    projects = PostgresProjectStore(db_engine)
    threads = PostgresThreadStore(db_engine)
    users = PostgresUserStore(db_engine)
    admins = PostgresAdminStore(db_engine)
    memory = PostgresMemoryStore(
        db_engine,
        caps={
            MemoryScope.THREAD: runtime_config.conversation.memory_cap_thread,
            MemoryScope.PROJECT: runtime_config.conversation.memory_cap_project,
            MemoryScope.USER: runtime_config.conversation.memory_cap_user,
        },
    )
    registry = ProviderRegistry(_bundles(runtime_config, cache))
    media_store, media_target = build_media_store(runtime_config)
    scan_parser = (
        DatalabClient(
            api_keys=runtime_config.ocr.api_keys,
            base_url=runtime_config.ocr.base_url,
            timeout=runtime_config.ocr.request_timeout_seconds,
        )
        if runtime_config.ocr.api_keys
        else None
    )
    # Every LLM node -- generation, understanding, summarization -- runs on the
    # same agent. They differ in model, prompt and whether they pass tools;
    # a second client would only be a second place for key rotation, usage
    # accounting and error sanitization to drift apart.
    llm_config = runtime_config.llm
    configured_routers = llm_config.configured_routers
    # Chat is configured when the *default* router has keys: the env-set
    # models are ids on that router, so keys for another router alone cannot
    # answer a turn that picked nothing.
    chat_is_configured = llm_config.default_router in configured_routers
    catalog = load_catalog()
    if chat_is_configured:
        default_spec = catalog.resolve(catalog.default)
        # Any other keyless router just hides its models from the picker,
        # but the catalog default is what every unknown or hidden model
        # degrades to; with its router unkeyed there is no floor left, and
        # every degraded turn would 401 mid-chat instead of failing here.
        if default_spec.router not in configured_routers:
            raise RuntimeError(
                f"Catalog default {default_spec.id!r} needs router "
                f"{default_spec.router!r}, which has no API keys"
            )
        hidden = [
            spec.id
            for spec in catalog.models
            if spec.enabled and spec.router not in configured_routers
        ]
        if hidden:
            _log.warning(
                "Models hidden from the picker, their router has no keys: %s",
                hidden,
            )
    chat = (
        BaseAgent(
            llm_config.routers,
            llm_config.model,
            default_router=llm_config.default_router,
            provider=llm_config.provider,
            timeout=llm_config.request_timeout_seconds,
        )
        if chat_is_configured
        else None
    )
    # A None answerer leaves create_query_app on MockAnswerer, which keeps
    # retrieval observable without spending generation tokens. `chat` is still
    # wired so thread streaming works either way. Its output cap follows the
    # configured model's catalog entry, same as the streaming route.
    answerer = (
        LLMAnswerer(
            chat,
            max_tokens=catalog.resolve(llm_config.model).max_output_tokens,
        )
        if chat is not None and not runtime_config.llm.use_mock_answerer
        else None
    )
    # Gated on the API key alone, not on `use_mock_answerer`: UnderstandMessage
    # still needs to extract memory when generation itself is mocked. Its own
    # model, so a paid model set for generation does not also get spent here.
    understander = (
        LLMUnderstander(
            BaseAgent(
                llm_config.routers,
                llm_config.understand_model,
                default_router=llm_config.default_router,
                provider=llm_config.provider,
                timeout=llm_config.request_timeout_seconds,
            ),
            max_tokens=runtime_config.conversation.understand_max_tokens,
        )
        if chat_is_configured
        else None
    )
    # Same gate and same reasoning as UnderstandMessage: its own cheap model,
    # and it keeps working when generation is mocked, because a thread that
    # grows past the window still needs its dropped turns carried forward.
    summarizer = (
        LLMSummarizer(
            BaseAgent(
                llm_config.routers,
                llm_config.summary_model,
                default_router=llm_config.default_router,
                provider=llm_config.provider,
                timeout=llm_config.request_timeout_seconds,
            ),
            max_tokens=runtime_config.conversation.summary_max_tokens,
        )
        if chat_is_configured
        else None
    )
    upload_executor = ThreadPoolExecutor(
        max_workers=runtime_config.upload.worker_count,
        thread_name_prefix="rag-upload",
    )
    auth_verifier = None
    if runtime_config.auth.firebase_service_account_path is not None:
        auth_verifier = FirebaseTokenVerifier.from_service_account(
            runtime_config.auth.firebase_service_account_path,
            project_id=runtime_config.auth.firebase_project_id,
        )
    app = create_query_app(
        client,
        registry,
        projects,
        answerer=answerer,
        media_store=media_store,
        scan_parser=scan_parser,
        # The same client: it renders and reads a deck through
        # `convert`, and OCRs a scanned page through `parse_pages`.
        converter=scan_parser,
        threads=threads,
        chat=chat,
        memory=memory,
        understand=understander,
        summarizer=summarizer,
        conversation=runtime_config.conversation,
        chunking=runtime_config.chunking,
        retrieval_config=runtime_config.retrieval,
        retrieval_cache=retrieval_cache,
        upload=runtime_config.upload,
        upload_executor=upload_executor,
        auth_verifier=auth_verifier,
        allow_x_user_id_fallback=runtime_config.auth.allow_x_user_id_fallback,
        users=users,
        admins=admins,
        admin_emails=runtime_config.auth.admin_emails,
        # None when nothing holds keys (mock mode): the picker then shows the
        # whole catalog instead of an empty list.
        configured_routers=configured_routers or None,
    )
    app.router.add_event_handler("shutdown", upload_executor.shutdown)
    if owns_redis_client and cache is not None:
        app.router.add_event_handler("shutdown", cache.close)
    if owns_qdrant_client:
        app.router.add_event_handler("shutdown", client.close)
    if owns_engine:
        app.router.add_event_handler("shutdown", db_engine.dispose)
    # Startup summary. Keys are reported as present or absent and never printed:
    # this goes to a log file, and a leaked provider key is worse than a missing
    # one. Most of what goes wrong at the edges is visible from exactly this.
    _log.info(
        "API ready. qdrant=%s prefix=%s providers=%s answerer=%s understand=%s "
        "summary=%s ocr=%s memory=%s embedding_cache=%s retrieval_cache=%s auth=%s "
        "media=%s log_level=%s log_sql=%s",
        runtime_config.qdrant_url,
        runtime_config.providers.collection_prefix,
        [provider.value for provider in registry.providers] or "none",
        "mock" if answerer is None else "llm",
        "off" if understander is None else runtime_config.llm.understand_model,
        "off" if summarizer is None else runtime_config.llm.summary_model,
        "datalab" if scan_parser is not None else "off",
        "on" if memory is not None else "off",
        "on" if cache is not None else "off",
        "on" if retrieval_cache is not None else "off",
        "on" if auth_verifier is not None else "off",
        media_target,
        runtime_config.logging.level,
        runtime_config.logging.sql,
    )
    if not registry.providers:
        _log.warning(
            "No embedding provider configured. Creating a project will answer 503; "
            "set COHERE_API_KEY, VOYAGE_API_KEY or JINA_API_KEY."
        )
    if scan_parser is None:
        _log.warning(
            "DATALAB_API_KEY is unset. Scanned PDFs and images will be rejected "
            "with 422; text-layer PDF, DOCX, XLSX and PPTX are unaffected."
        )

    app.state.engine = db_engine
    app.state.project_store = projects
    app.state.thread_store = threads
    app.state.memory_store = memory
    app.state.provider_registry = registry
    app.state.media_store = media_store
    app.state.redis = cache
    app.state.retrieval_cache = retrieval_cache
    app.state.chat = chat
    app.state.understand = understander
    app.state.summarizer = summarizer
    app.state.upload_executor = upload_executor
    app.state.auth_verifier = auth_verifier
    return app


app = create_runtime_app()
