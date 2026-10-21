from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from rag_ingestion.api.app import RuntimeConfig, create_runtime_app
from rag_ingestion.config import LLMConfig, ProviderConfig, RouterConfig
from rag_ingestion.indexing.local_embedder import LocalHashEmbedder
from rag_ingestion.providers.embedding_cache import RedisCachedEmbedder
from rag_ingestion.providers.registry import Provider


def _keyed_llm(**overrides) -> LLMConfig:
    """An LLMConfig with both routers keyed, the way `from_env` builds one
    when OPENROUTER_API_KEY and ORCAROUTER_API_KEY are set. Both, because the
    bundled catalog's default lives on orcarouter and boot refuses a keyless
    default -- the model every degraded turn lands on."""
    return LLMConfig(
        routers={
            "openrouter": RouterConfig(
                base_url="https://openrouter.ai/api/v1", api_keys=["test-key"]
            ),
            "orcarouter": RouterConfig(
                base_url="https://api.orcarouter.ai/v1", api_keys=["orca-key"]
            ),
        },
        **overrides,
    )


def test_local_hash_embedder_is_deterministic_and_has_configured_dimensions(pg_engine) -> None:
    embedder = LocalHashEmbedder(dimensions=8)
    profile = embedder.profile

    documents = embedder.embed_documents(
        ["Travel code ZX-42"], profile, intent="document"
    )
    query = embedder.embed_query("Travel code ZX-42", profile, intent="query")

    assert documents == [query]
    assert len(query) == 8
    assert any(value != 0 for value in query)


def test_qdrant_api_key_reaches_the_client_the_app_opens(pg_engine, monkeypatch) -> None:
    """Without this the app talks to a managed cluster anonymously and every
    call comes back 403, which reads like an empty collection rather than a
    credential that was configured and then dropped on the floor."""
    from rag_ingestion.api import app as app_module

    captured: dict[str, object] = {}

    def recorder(**kwargs: object) -> QdrantClient:
        captured.update(kwargs)
        return QdrantClient(location=":memory:")

    monkeypatch.setattr(app_module, "QdrantClient", recorder)
    config = RuntimeConfig(
        qdrant_url="https://cluster.cloud.qdrant.io:6333",
        qdrant_api_key="qdrant-secret",
        providers=ProviderConfig(collection_prefix="rag"),
    )

    create_runtime_app(config, engine=pg_engine, create_tables=False)

    assert captured == {
        "url": "https://cluster.cloud.qdrant.io:6333",
        "api_key": "qdrant-secret",
    }


def test_runtime_app_exposes_project_openapi_without_credentials_or_collections(pg_engine) -> None:
    qdrant = QdrantClient(location=":memory:")
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
    )
    app = create_runtime_app(config, qdrant_client=qdrant, engine=pg_engine)
    client = TestClient(app)

    redoc = client.get("/redoc")
    openapi = client.get("/openapi.json")
    health = client.get("/health")

    assert redoc.status_code == 200
    assert openapi.status_code == 200
    assert set(openapi.json()["paths"]) == {
        "/admin/admins",
        "/admin/admins/{email}",
        "/admin/audit",
        "/admin/me",
        "/admin/reports",
        "/admin/reports/{report_id}/resolve",
        "/admin/stats",
        "/admin/users",
        "/admin/users/{uid}/disable",
        "/admin/users/{uid}/enable",
        "/auth/claim",
        "/health",
        "/projects",
        "/projects/{project_id}",
        "/projects/{project_id}/answer",
        "/projects/{project_id}/documents",
        "/projects/{project_id}/documents/{file_id}",
        "/projects/{project_id}/documents/{doc_id}/markdown",
        "/projects/{project_id}/media/{key}",
        "/projects/{project_id}/members",
        "/projects/{project_id}/query",
        "/projects/{project_id}/transfer",
        "/reports",
        "/share/{share_id}",
        "/share/{share_id}/fork",
        "/shared",
        "/threads",
        "/threads/{thread_id}",
        "/threads/{thread_id}/compact",
        "/threads/{thread_id}/fork",
        "/threads/{thread_id}/messages",
        "/threads/{thread_id}/share",
        "/threads/{thread_id}/share/recipients",
        "/memory",
        "/memory/{memory_id}",
        "/memory/proposals",
        "/memory/proposals/{proposal_id}",
        "/memory/proposals/{proposal_id}/confirm",
        "/models",
        "/settings",
        "/usage",
    }
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "configured_providers": []}
    assert qdrant.get_collections().collections == []


def test_thread_routes_report_no_chat_provider_when_openrouter_is_unset(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
    )
    app = create_runtime_app(config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine)
    client = TestClient(app)

    created = client.post("/threads", json={}, headers={"X-User-ID": "user-a"})
    streamed = client.post(
        f"/threads/{created.json()['thread_id']}/messages",
        json={"content": "Hi"},
        headers={"X-User-ID": "user-a"},
    )

    assert app.state.chat is None
    assert app.state.understand is None
    assert created.status_code == 201
    assert streamed.status_code == 503


def test_openrouter_credentials_wire_a_chat_provider_and_real_answerer(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
        llm=_keyed_llm(),
    )
    app = create_runtime_app(config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine)

    assert app.state.chat is not None
    assert app.state.chat.model == "openrouter/free"
    assert app.state.understand is not None


def test_mock_answerer_can_be_forced_while_keeping_chat_streaming(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
        llm=_keyed_llm(model="anthropic/claude-sonnet-5", use_mock_answerer=True),
    )
    app = create_runtime_app(config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine)

    assert app.state.chat is not None
    assert app.state.chat.model == "anthropic/claude-sonnet-5"
    # Understanding must survive a mocked answerer: memory extraction is its
    # job regardless of whether generation itself is real.
    assert app.state.understand is not None


def test_understand_model_can_differ_from_the_generation_model(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
        llm=_keyed_llm(model="anthropic/claude-sonnet-5", understand_model="openrouter/free"),
    )
    app = create_runtime_app(config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine)

    assert app.state.chat.model == "anthropic/claude-sonnet-5"
    assert app.state.understand._chat.model == "openrouter/free"


def test_summarizer_is_wired_and_takes_its_own_model(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
        llm=_keyed_llm(model="anthropic/claude-sonnet-5", summary_model="vendor/cheap-summarizer", use_mock_answerer=True),
    )
    app = create_runtime_app(
        config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine
    )

    # Like understanding, summarizing outlives a mocked answerer: a thread that
    # outgrows the window still needs its dropped turns carried forward.
    assert app.state.summarizer is not None
    assert app.state.summarizer._chat.model == "vendor/cheap-summarizer"


def test_no_openrouter_key_leaves_the_summarizer_off(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="rag"),
    )
    app = create_runtime_app(
        config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine
    )

    assert app.state.summarizer is None


def test_runtime_registers_only_providers_with_credentials(pg_engine) -> None:
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        providers=ProviderConfig(collection_prefix="evidence", cohere_api_keys=["cohere-secret"]),
    )
    client = TestClient(
        create_runtime_app(config, qdrant_client=QdrantClient(location=":memory:"), engine=pg_engine)
    )

    assert client.get("/health").json()["configured_providers"] == ["cohere"]
    created = client.post(
        "/projects",
        headers={"X-User-ID": "user-a"},
        json={"name": "Research", "provider": "cohere"},
    )
    assert created.status_code == 201
    assert created.json()["embedding_profile"]["model"] == "embed-v4.0"


def test_runtime_wraps_provider_embedders_when_redis_is_configured(pg_engine) -> None:
    redis = object()
    config = RuntimeConfig(
        qdrant_url="http://unused:6333",
        redis_url="redis://unused:6379/0",
        providers=ProviderConfig(collection_prefix="evidence", cohere_api_keys=["cohere-secret"]),
    )

    app = create_runtime_app(
        config,
        qdrant_client=QdrantClient(location=":memory:"),
        engine=pg_engine,
        redis_client=redis,
    )

    bundle = app.state.provider_registry.get(Provider.COHERE)
    assert isinstance(bundle.embedder, RedisCachedEmbedder)
    assert app.state.redis is redis
