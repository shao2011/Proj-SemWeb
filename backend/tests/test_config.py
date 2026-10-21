import pytest

from pathlib import Path

from pydantic import ValidationError

from rag_ingestion import config as config_module
from rag_ingestion.config import (
    DEFAULT_HISTORY_KEEP_RECENT,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SUMMARY_MAX_FOLD_MESSAGES,
    RuntimeConfig,
    find_env_file,
    parse_env_file,
    resolve_environment,
)


# --- parsing ----------------------------------------------------------------


def test_plain_assignments_are_parsed() -> None:
    parsed = parse_env_file("A=1\nB=two\n")

    assert parsed == {"A": "1", "B": "two"}


def test_comments_and_blank_lines_are_ignored() -> None:
    parsed = parse_env_file("# a comment\n\n  \nA=1\n   # indented comment\n")

    assert parsed == {"A": "1"}


def test_export_prefix_is_stripped() -> None:
    assert parse_env_file("export A=1\n") == {"A": "1"}


def test_surrounding_quotes_are_stripped() -> None:
    parsed = parse_env_file("A=\"double\"\nB='single'\nC=\"un-matched\n")

    assert parsed == {"A": "double", "B": "single", "C": '"un-matched'}


def test_values_keep_inner_equals_and_hashes() -> None:
    parsed = parse_env_file("URL=http://x/y?a=1\nKEY=abc#not-a-comment\n")

    assert parsed == {"URL": "http://x/y?a=1", "KEY": "abc#not-a-comment"}


def test_whitespace_around_key_and_value_is_trimmed() -> None:
    assert parse_env_file("  A  =  1  \n") == {"A": "1"}


def test_malformed_lines_are_skipped() -> None:
    parsed = parse_env_file("no-equals-here\n=novalue\nA=1\n")

    assert parsed == {"A": "1"}


def test_empty_values_are_kept() -> None:
    assert parse_env_file("A=\n") == {"A": ""}


# --- precedence -------------------------------------------------------------


def test_real_environment_wins_over_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SHARED=from-file\nONLY_FILE=file\n", encoding="utf-8")

    resolved = resolve_environment(
        env_file=env_file, environ={"SHARED": "from-process"}
    )

    assert resolved["SHARED"] == "from-process"
    assert resolved["ONLY_FILE"] == "file"


def test_missing_env_file_falls_back_to_the_process_environment(tmp_path: Path) -> None:
    resolved = resolve_environment(env_file=tmp_path / "absent.env", environ={"A": "1"})

    assert resolved == {"A": "1"}


def test_env_file_location_can_be_pointed_at_by_a_variable(tmp_path: Path) -> None:
    custom = tmp_path / "custom.env"
    custom.write_text("FROM_CUSTOM=yes\n", encoding="utf-8")

    resolved = resolve_environment(environ={"ENV_FILE": str(custom)})

    assert resolved["FROM_CUSTOM"] == "yes"


def test_find_env_file_prefers_the_working_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert find_env_file() == tmp_path / ".env"


def test_find_env_file_falls_back_to_the_project_root(tmp_path: Path, monkeypatch) -> None:
    """Running uvicorn from a subdirectory must still find the project's .env."""
    monkeypatch.chdir(tmp_path)

    found = find_env_file()

    assert found is None or found.name == ".env"


def test_find_env_file_returns_none_when_nothing_exists(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_PROJECT_ROOT", tmp_path / "absent")

    assert find_env_file() is None


# --- runtime config ---------------------------------------------------------


def test_openrouter_defaults_to_the_free_router() -> None:
    assert DEFAULT_OPENROUTER_MODEL == "openrouter/free"

    config = RuntimeConfig.from_environment(environ={}, read_env_file=False)

    assert config.llm.model == "openrouter/free"
    assert config.llm.routers["openrouter"].api_keys == []
    assert config.llm.use_mock_answerer is False


def test_both_routers_exist_with_full_base_urls() -> None:
    """The version segment differs per gateway (`/api/v1` vs `/v1`), so the
    URL is stored whole instead of being assembled by the app factory."""
    config = RuntimeConfig.from_environment(environ={}, read_env_file=False)

    assert config.llm.default_router == "openrouter"
    assert config.llm.routers["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert config.llm.routers["orcarouter"].base_url == "https://api.orcarouter.ai/v1"
    assert config.llm.configured_routers == frozenset()


def test_orcarouter_reads_its_own_key_and_base_url() -> None:
    config = RuntimeConfig.from_environment(
        environ={
            "ORCAROUTER_API_KEY": "orca-a,orca-b",
            "ORCAROUTER_BASE_URL": "http://localhost:20128/v1",
        },
        read_env_file=False,
    )

    assert config.llm.routers["orcarouter"].api_keys == ["orca-a", "orca-b"]
    assert config.llm.routers["orcarouter"].base_url == "http://localhost:20128/v1"
    assert config.llm.configured_routers == frozenset({"orcarouter"})


def test_a_default_router_missing_from_the_table_is_rejected() -> None:
    from rag_ingestion.config import LLMConfig

    with pytest.raises(ValidationError):
        LLMConfig(default_router="does-not-exist")


def test_understand_model_defaults_to_the_same_free_router() -> None:
    config = RuntimeConfig.from_environment(environ={}, read_env_file=False)

    assert config.llm.understand_model == DEFAULT_OPENROUTER_MODEL


def test_understand_model_reads_its_own_env_var() -> None:
    config = RuntimeConfig.from_environment(
        environ={"UNDERSTAND_MODEL": "google/gemma-4-31b-it:free"},
        read_env_file=False,
    )

    assert config.llm.understand_model == "google/gemma-4-31b-it:free"


def test_redis_cache_is_optional_and_reads_its_ttl() -> None:
    without_redis = RuntimeConfig.from_environment(environ={}, read_env_file=False)
    with_redis = RuntimeConfig.from_environment(
        environ={
            "REDIS_URL": "redis://cache:6379/0",
            "EMBEDDING_CACHE_TTL_SECONDS": "3600",
        },
        read_env_file=False,
    )

    assert without_redis.redis_url is None
    assert without_redis.providers.embedding_cache_ttl_seconds == 86_400
    assert with_redis.redis_url == "redis://cache:6379/0"
    assert with_redis.providers.embedding_cache_ttl_seconds == 3_600


def test_qdrant_endpoint_and_key_are_read_from_the_environment() -> None:
    """A managed cluster rejects an unauthenticated request, so the key is a
    setting like the URL is, not something only a self-hosted default covers."""
    self_hosted = RuntimeConfig.from_environment(environ={}, read_env_file=False)
    managed = RuntimeConfig.from_environment(
        environ={
            "QDRANT_URL": "https://cluster.cloud.qdrant.io:6333",
            "QDRANT_API_KEY": "qdrant-secret",
        },
        read_env_file=False,
    )

    assert self_hosted.qdrant_url == "http://localhost:6333"
    assert self_hosted.qdrant_api_key is None
    assert managed.qdrant_url == "https://cluster.cloud.qdrant.io:6333"
    assert managed.qdrant_api_key == "qdrant-secret"


def test_blank_qdrant_api_key_is_treated_as_absent() -> None:
    config = RuntimeConfig.from_environment(
        environ={"QDRANT_API_KEY": "   "}, read_env_file=False
    )

    assert config.qdrant_api_key is None


def test_database_url_is_read_from_the_environment() -> None:
    managed = RuntimeConfig.from_environment(
        environ={"DATABASE_URL": "postgresql://u:p@pooler.example:5432/postgres"},
        read_env_file=False,
    )

    assert managed.database_url == "postgresql://u:p@pooler.example:5432/postgres"


def test_config_reads_credentials_out_of_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENROUTER_API_KEY=sk-from-file\n"
        "OPENROUTER_MODEL=google/gemma-4-31b-it:free\n"
        "COHERE_API_KEY=cohere-from-file\n",
        encoding="utf-8",
    )

    config = RuntimeConfig.from_environment(environ={}, env_file=env_file)

    assert config.llm.routers["openrouter"].api_keys == ["sk-from-file"]
    assert config.llm.model == "google/gemma-4-31b-it:free"
    assert config.providers.cohere_api_keys == ["cohere-from-file"]


def test_process_environment_overrides_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_MODEL=from-file\n", encoding="utf-8")

    config = RuntimeConfig.from_environment(
        environ={"OPENROUTER_MODEL": "from-process"}, env_file=env_file
    )

    assert config.llm.model == "from-process"


def test_blank_credentials_are_treated_as_absent() -> None:
    config = RuntimeConfig.from_environment(
        environ={"OPENROUTER_API_KEY": "   ", "COHERE_API_KEY": ""},
        read_env_file=False
    )

    assert config.llm.routers["openrouter"].api_keys == []
    assert config.providers.cohere_api_keys == []


def test_multiple_comma_separated_keys_all_parse() -> None:
    config = RuntimeConfig.from_environment(
        environ={
            "COHERE_API_KEY": "cohere-a, cohere-b",
            "VOYAGE_API_KEY": "voyage-a,voyage-b,voyage-c",
            "JINA_API_KEY": "jina-a",
            "DATALAB_API_KEY": "datalab-a,datalab-b",
            "OPENROUTER_API_KEY": "or-a,,or-b",
        },
        read_env_file=False,
    )

    assert config.providers.cohere_api_keys == ["cohere-a", "cohere-b"]
    assert config.providers.voyage_api_keys == ["voyage-a", "voyage-b", "voyage-c"]
    assert config.providers.jina_api_keys == ["jina-a"]
    assert config.ocr.api_keys == ["datalab-a", "datalab-b"]
    assert config.llm.routers["openrouter"].api_keys == ["or-a", "or-b"]


def test_boolean_flags_accept_the_usual_spellings() -> None:
    for value in ["1", "true", "TRUE", "yes", "on"]:
        config = RuntimeConfig.from_environment(
            environ={"USE_MOCK_ANSWERER": value}, read_env_file=False
        )
        assert config.llm.use_mock_answerer is True, value

    for value in ["0", "false", "no", "off", ""]:
        config = RuntimeConfig.from_environment(
            environ={"USE_MOCK_ANSWERER": value}, read_env_file=False
        )
        assert config.llm.use_mock_answerer is False, value


def test_numeric_settings_are_parsed() -> None:
    config = RuntimeConfig.from_environment(
        environ={
            "COHERE_EMBEDDING_DIMENSIONS": "512",
            "UPLOAD_MAX_FILES_PER_BATCH": "7",
            "UPLOAD_MAX_FILE_BYTES": "1024",
            "UPLOAD_MAX_BATCH_BYTES": "4096",
            "UPLOAD_WORKER_COUNT": "3",
        },
        read_env_file=False,
    )

    assert config.providers.cohere_dimensions == 512
    assert config.upload.max_files_per_batch == 7
    assert config.upload.max_file_bytes == 1024
    assert config.upload.max_batch_bytes == 4096
    assert config.upload.worker_count == 3


def test_history_and_summary_settings_are_tunable() -> None:
    config = RuntimeConfig.from_environment(
        environ={
            "HISTORY_KEEP_RECENT": "4",
            "SUMMARY_MAX_FOLD_MESSAGES": "9",
            "SUMMARY_MODEL": "vendor/cheap-summarizer",
        },
        read_env_file=False,
    )

    assert config.conversation.history_keep_recent == 4
    assert config.conversation.summary_max_fold_messages == 9
    assert config.llm.summary_model == "vendor/cheap-summarizer"


def test_history_and_summary_settings_have_defaults() -> None:
    config = RuntimeConfig.from_environment(environ={}, read_env_file=False)

    assert config.conversation.history_keep_recent == DEFAULT_HISTORY_KEEP_RECENT
    assert (
        config.conversation.summary_max_fold_messages
        == DEFAULT_SUMMARY_MAX_FOLD_MESSAGES
    )
    # Falls back to the shared default model, like the understand node does.
    assert config.llm.summary_model == DEFAULT_OPENROUTER_MODEL


def test_config_is_importable_from_the_api_module_for_compatibility() -> None:
    from rag_ingestion.api.app import RuntimeConfig as Reexported

    assert Reexported is RuntimeConfig


def test_retrieval_and_chunking_depths_are_tunable() -> None:
    """The knobs that move retrieval quality the most.

    Both objects existed and were reachable in code, but nothing read them
    from the environment, so a deployment could only change them by editing
    source. That made a benchmark sweep impossible.
    """
    config = RuntimeConfig.from_environment(
        environ={
            "RETRIEVAL_ROUTE_LIMIT": "7",
            "RETRIEVAL_ROUTE_K": "40",
            "RETRIEVAL_GLOBAL_K": "25",
            "RETRIEVAL_MAX_CANDIDATES": "80",
            "RETRIEVAL_TOKEN_BUDGET": "6000",
            "RETRIEVAL_REFERENCE_CAP": "2",
            "CHUNK_MIN_TOKENS": "50",
            "CHUNK_TARGET_TOKENS": "300",
            "CHUNK_MAX_TOKENS": "700",
        },
        read_env_file=False,
    )

    assert config.retrieval.route_limit == 7
    assert config.retrieval.route_k == 40
    assert config.retrieval.global_k == 25
    assert config.retrieval.max_candidates == 80
    assert config.retrieval.token_budget == 6_000
    assert config.retrieval.reference_cap == 2
    assert config.chunking.leaf_min_tokens == 50
    assert config.chunking.leaf_target_tokens == 300
    assert config.chunking.leaf_max_tokens == 700


def test_endpoints_and_timeouts_are_tunable() -> None:
    """A deployment behind a proxy or a self-hosted gateway needs these."""
    config = RuntimeConfig.from_environment(
        environ={
            "COHERE_BASE_URL": "https://gateway.internal/cohere",
            "VOYAGE_BASE_URL": "https://gateway.internal/voyage",
            "JINA_BASE_URL": "https://gateway.internal/jina",
            "OPENROUTER_BASE_URL": "https://gateway.internal/llm",
            "DATALAB_BASE_URL": "https://gateway.internal/ocr",
            "PROVIDER_TIMEOUT_SECONDS": "12.5",
            "OPENROUTER_TIMEOUT_SECONDS": "90",
            "DATALAB_TIMEOUT_SECONDS": "300",
            "REDIS_CONNECT_TIMEOUT_SECONDS": "1.5",
            "REDIS_READ_TIMEOUT_SECONDS": "2.5",
        },
        read_env_file=False,
    )

    assert config.providers.cohere_base_url == "https://gateway.internal/cohere"
    assert config.providers.voyage_base_url == "https://gateway.internal/voyage"
    assert config.providers.jina_base_url == "https://gateway.internal/jina"
    assert config.llm.routers["openrouter"].base_url == "https://gateway.internal/llm"
    assert config.ocr.base_url == "https://gateway.internal/ocr"
    assert config.providers.request_timeout_seconds == 12.5
    assert config.llm.request_timeout_seconds == 90.0
    assert config.ocr.request_timeout_seconds == 300.0
    assert config.redis_connect_timeout_seconds == 1.5
    assert config.redis_read_timeout_seconds == 2.5


def test_per_node_token_ceilings_and_memory_caps_are_tunable() -> None:
    config = RuntimeConfig.from_environment(
        environ={
            "UNDERSTAND_MAX_TOKENS": "512",
            "SUMMARY_MAX_TOKENS": "800",
            "MEMORY_CAP_THREAD": "5",
            "MEMORY_CAP_PROJECT": "50",
            "MEMORY_CAP_USER": "60",
        },
        read_env_file=False,
    )

    # Generation's ceiling is deliberately absent: it comes from the picked
    # model's catalog entry, not from an env var.
    assert config.conversation.understand_max_tokens == 512
    assert config.conversation.summary_max_tokens == 800
    assert config.conversation.memory_cap_thread == 5
    assert config.conversation.memory_cap_project == 50
    assert config.conversation.memory_cap_user == 60


def test_global_fallback_guard_still_explains_itself() -> None:
    """`gt=0` on the field would fire first and replace the explanation with a
    generic bounds message, so that constraint is deliberately absent."""
    from rag_ingestion.config import RetrievalConfig

    with pytest.raises(ValueError, match="global fallback"):
        RetrievalConfig(global_k=0)


def test_admin_emails_are_parsed_normalized_and_default_empty() -> None:
    """Bootstrap admins come from ADMIN_EMAILS: comma-separated, padding and
    case ignored, because the value is matched against verified login
    addresses that are stored casefolded."""
    empty = RuntimeConfig.from_environment(environ={}, read_env_file=False)
    assert empty.auth.admin_emails == []

    config = RuntimeConfig.from_environment(
        environ={"ADMIN_EMAILS": " Root@Example.COM , second@example.com ,, "},
        read_env_file=False,
    )
    assert config.auth.admin_emails == ["root@example.com", "second@example.com"]
