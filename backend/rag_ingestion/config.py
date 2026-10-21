"""Single place every runtime setting is read from.

Values come from the process environment first and a `.env` file second, so
Docker Compose (which injects real environment variables) and a bare
`uvicorn` run against the same file both work without a code change.

No variable interpolation and no shell semantics: this reads `KEY=value`
lines, nothing more. Anything richer belongs in the process environment.

Settings are grouped into sub-models **by consumer, not by topic**: a group
exists when one component takes the whole of it, so `create_query_app` can
receive `conversation` and `upload` instead of eight loose integers. Grouping
by topic instead would produce tidy-looking classes that no caller ever passes
around whole.

The environment variable names are the public interface (`.env`, compose.yaml
and any deployment depend on them), so they are deliberately unchanged and
stay flat. Only the shape on the Python side is nested.

Not everything numeric belongs here. A value earns a place when an operator
has a real reason to change it per deployment. Thresholds that define whether
an algorithm is correct (`structure.MIN_COVERAGE` and its neighbours) stay
next to the code they constrain: exposing them would invite a setting that
silently breaks detection rather than tuning it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import Field, model_validator

from rag_ingestion.models import StrictModel

DEFAULT_OPENROUTER_MODEL = "openrouter/free"

#: Full URL up to and including the version segment. The two gateways disagree
#: on it -- OpenRouter serves under `/api/v1`, OrcaRouter under `/v1` -- which
#: is why the app factory no longer builds it by concatenation.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_ORCAROUTER_BASE_URL = "https://api.orcarouter.ai/v1"

#: Which LangChain provider builds the chat client. "openai" covers OpenRouter
#: and anything else speaking that wire format; "anthropic", "google_genai" and
#: "ollama" reach those APIs natively. Lives here rather than next to the
#: builder so `config` stays the bottom of the import graph.
DEFAULT_LLM_PROVIDER = "openai"

# Loud by default. Logging exists here to be read while the pipeline is being
# worked on, so the quiet setting is the one you opt into.
DEFAULT_LOG_LEVEL = "DEBUG"

# Matches the published port of the `db` service in compose.yaml, so host-side
# tooling (pytest, alembic) works with no configuration.
DEFAULT_DATABASE_URL = "postgresql://postgres:password@localhost:5432/rag"

DEFAULT_UPLOAD_MAX_FILES_PER_BATCH = 10
DEFAULT_UPLOAD_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_UPLOAD_MAX_BATCH_BYTES = 100 * 1024 * 1024
DEFAULT_UPLOAD_WORKER_COUNT = 2
# How long a trashed document stays restorable-for-free. Within this window a
# re-upload of identical content flips the row back instead of re-embedding;
# past it the trashed row is purged lazily and the upload indexes from scratch.
DEFAULT_UPLOAD_TRASH_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_EMBEDDING_CACHE_TTL_SECONDS = 24 * 60 * 60
# Whole-outcome cache. Far shorter than the embedding TTL: an embedding is
# a pure function of (text, model) and never goes stale, while a retrieval
# outcome is only valid for the documents that existed when it ran.
DEFAULT_RETRIEVAL_CACHE_TTL_SECONDS = 15 * 60

# The floor of the replayed window: at least this many of the most recent
# messages survive verbatim even when the model's budget disagrees, because a
# reply needs its immediate context. The *ceiling* is no longer a config knob:
# it comes from the picked model's entry in `data/models.json` (message cap
# and context budget), sized per turn in `deps.preflight_turns`.
DEFAULT_HISTORY_KEEP_RECENT = 6
# Ceiling on one fold. The watermark can fall a long way behind, a provider
# outage over many turns is enough, and without a cap the recovery fold would
# hand the summarizer the whole backlog in a single call.
DEFAULT_SUMMARY_MAX_FOLD_MESSAGES = 40
# The binding ceiling on one fold: 40 RAG-sized answers can dwarf the summary
# model's own context while 40 short turns are nothing, so the fetch is capped
# by count and the fold itself by chars. ~13k tokens at Vietnamese's 2.5
# chars/token, well inside any summary model.
DEFAULT_SUMMARY_MAX_FOLD_CHARS = 32_000
# The summary is a list of items now, so the ceiling counts items rather than
# words. A count makes eviction a discrete, logged decision; the old 150-word
# prompt rule made it a continuous, invisible one -- and told the model to
# compress the oldest material first, which is how early facts died (ACE,
# ICLR 2026, calls this context collapse). Thirty-two items cost ~1.3k tokens
# rendered -- 0.13% of a 1M window, 4% of the free tier's.
DEFAULT_SUMMARY_MAX_ITEMS = 32
DEFAULT_SUMMARY_ITEM_MAX_CHARS = 160
# One fold covers at most DEFAULT_SUMMARY_MAX_FOLD_MESSAGES messages, so a
# delta asking for more changes than this is a malfunction, not thoroughness.
DEFAULT_SUMMARY_MAX_OPS = 20

# UnderstandMessage's context window. Four rather than six, and even rather
# than odd: the slice counts messages, so an odd number cuts a question away
# from the answer it produced. Four is two question/answer pairs, which is as
# far back as a pronoun realistically reaches.
DEFAULT_UNDERSTAND_CONTEXT_MESSAGES = 4
DEFAULT_UNDERSTAND_CONTEXT_CHARS = 1_500
# The thread summary gets its own budget rather than sharing the one above.
# Sharing would let a long summary evict the most recent turns, which are the
# stronger signal for resolving a reference.
DEFAULT_UNDERSTAND_SUMMARY_CHARS = 800

# How many memories a prompt carries. Counted in items rather than characters:
# a character budget silently made the cut depend on how wordy the neighbouring
# memories happened to be.
DEFAULT_MEMORY_MAX_ITEMS = 25
# Backstop under the item cap, for the case where a few very long values would
# otherwise flood the system message. Memory is injected on every turn, so this
# is a permanent cost and the overflow is reported rather than dropped.
DEFAULT_MEMORY_CHAR_BUDGET = 2_000

# Design §6.5: the same key written in enough different threads is the
# evidence that a situational preference is really a habit. Five, not two: a
# single repeat is coincidence, and a wrong promotion is a global memory the
# user never asked for.
DEFAULT_PROMOTION_THREAD_THRESHOLD = 5

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Candidates are fixed rather than an upward walk: an unbounded search can
# silently pick up an unrelated `.env` from a parent directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_firebase_service_account_path() -> str | None:
    # Files only: a `docker compose up` run while the credential was missing
    # leaves an empty *directory* under the mount's name, and globbing that
    # up would fail every construction instead of just disabling auth.
    matches = sorted(
        path for path in _PROJECT_ROOT.glob("*firebase-adminsdk-*.json")
        if path.is_file()
    )
    if not matches:
        return None
    return str(matches[0])


def parse_keys(raw: str | None) -> list[str]:
    """Comma-separated keys, trimmed, blanks dropped. `None` and `""` -> `[]`."""
    if not raw:
        return []
    return [key.strip() for key in raw.split(",") if key.strip()]


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def find_env_file(extra_candidates: Iterable[Path] | None = None) -> Path | None:
    candidates = [
        *(extra_candidates or []),
        Path.cwd() / ".env",
        _PROJECT_ROOT / ".env",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def resolve_environment(
    *,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    read_env_file: bool = True,
) -> dict[str, str]:
    """Merge the process environment over a `.env` file.

    Real environment variables win, so an injected value is never shadowed by
    a stale file. Pass `read_env_file=False` to ignore files entirely; without
    it, omitting `env_file` means "search for one", not "use none", which would
    otherwise make an isolated caller silently pick up the project's own `.env`.
    """
    resolved = dict(os.environ if environ is None else environ)
    path: Path | None = None
    if not read_env_file:
        path = None
    elif env_file is not None:
        path = Path(env_file)
    elif resolved.get("ENV_FILE"):
        path = Path(resolved["ENV_FILE"])
    else:
        path = find_env_file()
    if path is not None and path.is_file():
        for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
            resolved.setdefault(key, value)
    return resolved


class ProviderConfig(StrictModel):
    """Everything `api.app._bundles` needs to build embedder/reranker pairs."""

    # v2: sparse values moved from raw term counts to BM25 TF (saturation +
    # length norm) and Thai gained char-gram tokenization. Old v1 collections
    # score sparse differently, so they are stranded by name and re-indexed
    # rather than mixed.
    profile_version: str = Field(default="v2", min_length=1)
    collection_prefix: str = Field(default="rag", min_length=1)
    #: Base URLs are settings, not constants: a deployment may need a regional
    #: endpoint, a corporate proxy, or a self-hosted gateway in front of the
    #: vendor, and a hardcoded host makes all three impossible.
    cohere_base_url: str = Field(default="https://api.cohere.com", min_length=1)
    voyage_base_url: str = Field(default="https://api.voyageai.com", min_length=1)
    jina_base_url: str = Field(default="https://api.jina.ai", min_length=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_cache_ttl_seconds: int = Field(
        default=DEFAULT_EMBEDDING_CACHE_TTL_SECONDS, gt=0
    )
    #: Comma-separated in the environment, parsed to a list; see `parse_keys`.
    #: Several keys round-robin, because embedding and reranking calls are
    #: independent of one another.
    cohere_api_keys: list[str] = Field(default_factory=list)
    cohere_embedding_model: str = "embed-v4.0"
    cohere_rerank_model: str = "rerank-v4.0-pro"
    cohere_dimensions: int = Field(default=1024, gt=0)
    voyage_api_keys: list[str] = Field(default_factory=list)
    voyage_embedding_model: str = "voyage-4"
    voyage_rerank_model: str = "rerank-2.5"
    voyage_dimensions: int = Field(default=1024, gt=0)
    jina_api_keys: list[str] = Field(default_factory=list)
    jina_embedding_model: str = "jina-embeddings-v4"
    jina_rerank_model: str = "jina-reranker-v3"
    jina_dimensions: int = Field(default=1024, gt=0)


class OcrConfig(StrictModel):
    """Scanned-page parsing. No keys means scanned input is rejected."""

    api_keys: list[str] = Field(default_factory=list)
    base_url: str = Field(default="https://www.datalab.to/api/v1/convert", min_length=1)
    request_timeout_seconds: float = Field(default=120.0, gt=0)


class RouterConfig(StrictModel):
    """One OpenAI-compatible gateway: where it lives, and its key pool.

    A router with no keys is a known destination that is not usable yet; the
    picker hides its models and `BaseAgent` refuses to route to it, but its
    presence here keeps the catalog's `router` names resolvable.
    """

    base_url: str = Field(min_length=1)
    api_keys: list[str] = Field(default_factory=list)


class LLMConfig(StrictModel):
    """The chat gateways and the three roles that draw on them.

    One key pool per router, three model names. The bookkeeping nodes get
    their own model on purpose: pointing generation at something expensive
    must not drag the cheap calls along with it.
    """

    routers: dict[str, RouterConfig] = Field(
        default_factory=lambda: {
            "openrouter": RouterConfig(base_url=DEFAULT_OPENROUTER_BASE_URL)
        }
    )
    #: Where a call goes when nothing picked a model: the env-configured
    #: `model`/`understand_model`/`summary_model` are ids on this router.
    default_router: str = Field(default="openrouter", min_length=1)
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    understand_model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    summary_model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    #: Which LangChain provider builds the client. "openai" covers OpenRouter
    #: and anything else speaking that wire format; "anthropic", "google_genai"
    #: and "ollama" reach those APIs natively. Changing provider is meant to be
    #: configuration, not a code edit.
    provider: str = Field(default=DEFAULT_LLM_PROVIDER, min_length=1)
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    use_mock_answerer: bool = False

    @model_validator(mode="after")
    def validate_default_router_exists(self) -> LLMConfig:
        if self.default_router not in self.routers:
            raise ValueError(
                f"default_router {self.default_router!r} is not a configured router"
            )
        return self

    @property
    def configured_routers(self) -> frozenset[str]:
        """Names of the routers that actually hold keys."""
        return frozenset(
            name for name, router in self.routers.items() if router.api_keys
        )


class ConversationConfig(StrictModel):
    """The shape of one turn: what is replayed, summarized, and extracted.

    Consumed whole by `create_query_app`, which is the reason this group
    exists rather than eight loose integers in its signature.
    """

    history_keep_recent: int = Field(default=DEFAULT_HISTORY_KEEP_RECENT, gt=0)
    summary_max_fold_messages: int = Field(
        default=DEFAULT_SUMMARY_MAX_FOLD_MESSAGES, gt=0
    )
    summary_max_fold_chars: int = Field(
        default=DEFAULT_SUMMARY_MAX_FOLD_CHARS, gt=0
    )
    #: Per-node output ceilings for the two bookkeeping nodes, which emit a
    #: small JSON object and a short paragraph. Generation's ceiling is not
    #: here: it comes from the picked model's `max_output_tokens` in the
    #: catalog, because 2k tokens that suited a free model truncates a
    #: thinking model mid-answer.
    understand_max_tokens: int = Field(default=256, gt=0)
    summary_max_tokens: int = Field(default=400, gt=0)
    understand_context_messages: int = Field(
        default=DEFAULT_UNDERSTAND_CONTEXT_MESSAGES, gt=0
    )
    understand_context_chars: int = Field(
        default=DEFAULT_UNDERSTAND_CONTEXT_CHARS, gt=0
    )
    #: Zero switches the summary off as U's context without touching the
    #: summary itself, which the prompt still uses.
    understand_summary_chars: int = Field(
        default=DEFAULT_UNDERSTAND_SUMMARY_CHARS, ge=0
    )
    memory_max_items: int = Field(default=DEFAULT_MEMORY_MAX_ITEMS, gt=0)
    memory_char_budget: int = Field(default=DEFAULT_MEMORY_CHAR_BUDGET, gt=0)
    #: Per-scope ceilings on stored memory, sized by how long the scope lives:
    #: a thread and a project end, user scope does not. A full scope evicts its
    #: oldest row rather than refusing the write.
    memory_cap_thread: int = Field(default=10, gt=0)
    memory_cap_project: int = Field(default=30, gt=0)
    #: Total user messages an anonymous identity may send before login is
    #: required. Zero disables the gate. The client mirrors this as a courtesy
    #: counter; this value is the authoritative one.
    anon_message_limit: int = Field(default=3, ge=0)
    memory_cap_user: int = Field(default=50, gt=0)
    promotion_thread_threshold: int = Field(
        default=DEFAULT_PROMOTION_THREAD_THRESHOLD, gt=0
    )


class ChunkingConfig(StrictModel):
    """Leaf sizing at ingest.

    Lives here rather than beside the chunker because it is a setting an
    operator tunes, and because `config` has to stay a leaf module: the
    chunker importing it is only safe while the dependency runs one way.
    """

    leaf_min_tokens: int = Field(default=100, ge=1)
    leaf_target_tokens: int = Field(default=256, ge=1)
    leaf_max_tokens: int = Field(default=512, ge=1)

    @model_validator(mode="after")
    def validate_token_limits(self) -> ChunkingConfig:
        if not self.leaf_min_tokens <= self.leaf_target_tokens <= self.leaf_max_tokens:
            raise ValueError("Token limits must satisfy min <= target <= max")
        return self


class RetrievalConfig(StrictModel):
    """Tunable depths for the query chain.

    `global_k` is deliberately always positive: a wrong soft route must never
    make the correct leaf unreachable.
    """

    route_limit: int = Field(default=5, gt=0)
    route_k: int = Field(default=30, gt=0)
    #: No `gt=0` here on purpose: a field constraint fires before the validator
    #: below and would replace its explanation with a generic bounds message.
    #: This one is worth explaining, so the validator owns it.
    global_k: int = 30
    max_candidates: int = Field(default=60, gt=0)
    token_budget: int = Field(default=4_000, gt=0)
    reference_cap: int = Field(default=5, ge=0)
    #: How far below the best reranked hit a candidate may score and still count
    #: as evidence. Relative, not absolute: a reranker's score is query-dependent
    #: and the three provider families here do not share a distribution, so an
    #: absolute floor would need recalibrating per query and per provider. The
    #: `le=1.0` bound is load-bearing -- above 1.0 the floor outruns the top hit
    #: and the chain returns no evidence at all. 0 keeps every hit.
    keep_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_global_fallback(self) -> RetrievalConfig:
        if self.global_k < 1:
            raise ValueError("global_k must stay positive to keep a global fallback")
        return self


class UploadConfig(StrictModel):
    max_files_per_batch: int = Field(default=DEFAULT_UPLOAD_MAX_FILES_PER_BATCH, gt=0)
    max_file_bytes: int = Field(default=DEFAULT_UPLOAD_MAX_FILE_BYTES, gt=0)
    max_batch_bytes: int = Field(default=DEFAULT_UPLOAD_MAX_BATCH_BYTES, gt=0)
    worker_count: int = Field(default=DEFAULT_UPLOAD_WORKER_COUNT, gt=0)
    trash_ttl_seconds: int = Field(default=DEFAULT_UPLOAD_TRASH_TTL_SECONDS, gt=0)


class LoggingConfig(StrictModel):
    level: str = Field(default=DEFAULT_LOG_LEVEL, min_length=1)
    #: Unset means stdout only. A path also gets a rotating file handler.
    file: str | None = None
    #: SQL statement logging, off by default: useful, and loud enough to bury
    #: everything else when it is not what you are looking for.
    sql: bool = False


class AuthConfig(StrictModel):
    """Authentication settings for Firebase ID token verification."""

    firebase_service_account_path: str | None = None
    firebase_project_id: str | None = None
    # Dev fallback for environments that still send only X-User-ID.
    allow_x_user_id_fallback: bool = True
    #: Bootstrap administrators, by verified email address. These exist before
    #: any database row does, which is what solves "who makes the first admin".
    #: They are also the floor: the API refuses to revoke or disable them, so a
    #: mistake in the admin screen cannot lock everybody out of it.
    admin_emails: list[str] = Field(default_factory=list)


class RuntimeConfig(StrictModel):
    """Infrastructure endpoints, plus one sub-model per consumer."""

    database_url: str = Field(default=DEFAULT_DATABASE_URL, min_length=1)
    qdrant_url: str = Field(min_length=1)
    #: Unset for a self-hosted cluster, which has no auth. A managed cluster
    #: answers 403 without it, and a 403 on search looks like an empty
    #: collection rather than a missing credential.
    qdrant_api_key: str | None = None
    redis_url: str | None = None
    #: Off by default. The whole-outcome cache only pays for itself when the
    #: same question recurs in the same project, which is a property of the
    #: deployment rather than of the code, so it is opt-in.
    retrieval_cache_enabled: bool = False
    retrieval_cache_ttl_seconds: int = Field(
        default=DEFAULT_RETRIEVAL_CACHE_TTL_SECONDS, gt=0
    )
    redis_connect_timeout_seconds: float = Field(default=0.2, gt=0)
    redis_read_timeout_seconds: float = Field(default=0.5, gt=0)
    media_root: str = Field(default="media", min_length=1)
    #: Object storage for media, reached over the S3 API. Unset everywhere
    #: means the local disk under `media_root`, which is fine for a single
    #: machine and loses every uploaded document the moment there are two.
    r2_endpoint: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    providers: ProviderConfig = Field(default_factory=ProviderConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @property
    def r2_credentials(self) -> tuple[str, str, str, str] | None:
        """The four R2 settings, or None when media stays on local disk."""
        values = (
            self.r2_endpoint,
            self.r2_bucket,
            self.r2_access_key_id,
            self.r2_secret_access_key,
        )
        if all(values):
            return values  # type: ignore[return-value]
        return None

    @model_validator(mode="after")
    def validate_media_backend(self) -> RuntimeConfig:
        # Half-configured object storage silently writes uploads to the
        # container's disk instead, where the next deploy takes them with it.
        # Refusing to start is the cheaper failure.
        names = (
            "R2_ENDPOINT",
            "R2_BUCKET",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
        )
        values = (
            self.r2_endpoint,
            self.r2_bucket,
            self.r2_access_key_id,
            self.r2_secret_access_key,
        )
        missing = [name for name, value in zip(names, values) if not value]
        if missing and len(missing) != len(names):
            raise ValueError(
                "R2 media storage needs every setting or none; missing "
                + ", ".join(missing)
            )
        return self

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
        read_env_file: bool = True,
    ) -> RuntimeConfig:
        env = resolve_environment(
            env_file=env_file, environ=environ, read_env_file=read_env_file
        )

        def text(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value or default

        def optional(name: str) -> str | None:
            return env.get(name, "").strip() or None

        def number(name: str, default: int) -> int:
            return int(text(name, str(default)))

        def decimal(name: str, default: float) -> float:
            return float(text(name, str(default)))

        def flag(name: str) -> bool:
            return env.get(name, "").strip().casefold() in _TRUE_VALUES

        return cls(
            database_url=text("DATABASE_URL", DEFAULT_DATABASE_URL),
            qdrant_url=text("QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=optional("QDRANT_API_KEY"),
            redis_url=optional("REDIS_URL"),
            retrieval_cache_enabled=flag("RETRIEVAL_CACHE_ENABLED"),
            retrieval_cache_ttl_seconds=number(
                "RETRIEVAL_CACHE_TTL_SECONDS", DEFAULT_RETRIEVAL_CACHE_TTL_SECONDS
            ),
            redis_connect_timeout_seconds=decimal("REDIS_CONNECT_TIMEOUT_SECONDS", 0.2),
            redis_read_timeout_seconds=decimal("REDIS_READ_TIMEOUT_SECONDS", 0.5),
            media_root=text("MEDIA_ROOT", "media"),
            r2_endpoint=optional("R2_ENDPOINT"),
            r2_bucket=optional("R2_BUCKET"),
            r2_access_key_id=optional("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=optional("R2_SECRET_ACCESS_KEY"),
            providers=ProviderConfig(
                profile_version=text("EMBEDDING_PROFILE_VERSION", "v2"),
                collection_prefix=text("QDRANT_COLLECTION_PREFIX", "rag"),
                cohere_base_url=text("COHERE_BASE_URL", "https://api.cohere.com"),
                voyage_base_url=text("VOYAGE_BASE_URL", "https://api.voyageai.com"),
                jina_base_url=text("JINA_BASE_URL", "https://api.jina.ai"),
                request_timeout_seconds=decimal("PROVIDER_TIMEOUT_SECONDS", 30.0),
                embedding_cache_ttl_seconds=number(
                    "EMBEDDING_CACHE_TTL_SECONDS",
                    DEFAULT_EMBEDDING_CACHE_TTL_SECONDS,
                ),
                cohere_api_keys=parse_keys(env.get("COHERE_API_KEY")),
                cohere_embedding_model=text("COHERE_EMBEDDING_MODEL", "embed-v4.0"),
                cohere_rerank_model=text("COHERE_RERANK_MODEL", "rerank-v4.0-pro"),
                cohere_dimensions=number("COHERE_EMBEDDING_DIMENSIONS", 1024),
                voyage_api_keys=parse_keys(env.get("VOYAGE_API_KEY")),
                voyage_embedding_model=text("VOYAGE_EMBEDDING_MODEL", "voyage-4"),
                voyage_rerank_model=text("VOYAGE_RERANK_MODEL", "rerank-2.5"),
                voyage_dimensions=number("VOYAGE_EMBEDDING_DIMENSIONS", 1024),
                jina_api_keys=parse_keys(env.get("JINA_API_KEY")),
                jina_embedding_model=text("JINA_EMBEDDING_MODEL", "jina-embeddings-v4"),
                jina_rerank_model=text("JINA_RERANK_MODEL", "jina-reranker-v3"),
                jina_dimensions=number("JINA_EMBEDDING_DIMENSIONS", 1024),
            ),
            ocr=OcrConfig(
                api_keys=parse_keys(env.get("DATALAB_API_KEY")),
                base_url=text(
                    "DATALAB_BASE_URL", "https://www.datalab.to/api/v1/convert"
                ),
                request_timeout_seconds=decimal("DATALAB_TIMEOUT_SECONDS", 120.0),
            ),
            llm=LLMConfig(
                # `*_BASE_URL` is the full prefix including the version
                # segment now (`/api/v1` vs `/v1`), not a host to append to.
                routers={
                    "openrouter": RouterConfig(
                        base_url=text(
                            "OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL
                        ),
                        api_keys=parse_keys(env.get("OPENROUTER_API_KEY")),
                    ),
                    "orcarouter": RouterConfig(
                        base_url=text(
                            "ORCAROUTER_BASE_URL", DEFAULT_ORCAROUTER_BASE_URL
                        ),
                        api_keys=parse_keys(env.get("ORCAROUTER_API_KEY")),
                    ),
                },
                model=text("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
                understand_model=text("UNDERSTAND_MODEL", DEFAULT_OPENROUTER_MODEL),
                summary_model=text("SUMMARY_MODEL", DEFAULT_OPENROUTER_MODEL),
                provider=text("LLM_PROVIDER", DEFAULT_LLM_PROVIDER),
                request_timeout_seconds=decimal("OPENROUTER_TIMEOUT_SECONDS", 120.0),
                use_mock_answerer=flag("USE_MOCK_ANSWERER"),
            ),
            conversation=ConversationConfig(
                history_keep_recent=number(
                    "HISTORY_KEEP_RECENT", DEFAULT_HISTORY_KEEP_RECENT
                ),
                summary_max_fold_messages=number(
                    "SUMMARY_MAX_FOLD_MESSAGES", DEFAULT_SUMMARY_MAX_FOLD_MESSAGES
                ),
                summary_max_fold_chars=number(
                    "SUMMARY_MAX_FOLD_CHARS", DEFAULT_SUMMARY_MAX_FOLD_CHARS
                ),
                understand_max_tokens=number("UNDERSTAND_MAX_TOKENS", 256),
                summary_max_tokens=number("SUMMARY_MAX_TOKENS", 400),
                understand_context_messages=number(
                    "UNDERSTAND_CONTEXT_MESSAGES", DEFAULT_UNDERSTAND_CONTEXT_MESSAGES
                ),
                understand_context_chars=number(
                    "UNDERSTAND_CONTEXT_CHARS", DEFAULT_UNDERSTAND_CONTEXT_CHARS
                ),
                understand_summary_chars=number(
                    "UNDERSTAND_SUMMARY_CHARS", DEFAULT_UNDERSTAND_SUMMARY_CHARS
                ),
                memory_max_items=number(
                    "MEMORY_MAX_ITEMS", DEFAULT_MEMORY_MAX_ITEMS
                ),
                memory_char_budget=number(
                    "MEMORY_CHAR_BUDGET", DEFAULT_MEMORY_CHAR_BUDGET
                ),
                memory_cap_thread=number("MEMORY_CAP_THREAD", 10),
                anon_message_limit=number("ANON_MESSAGE_LIMIT", 3),
                memory_cap_project=number("MEMORY_CAP_PROJECT", 30),
                memory_cap_user=number("MEMORY_CAP_USER", 50),
                promotion_thread_threshold=number(
                    "PROMOTION_THREAD_THRESHOLD", DEFAULT_PROMOTION_THREAD_THRESHOLD
                ),
            ),
            chunking=ChunkingConfig(
                leaf_min_tokens=number("CHUNK_MIN_TOKENS", 100),
                leaf_target_tokens=number("CHUNK_TARGET_TOKENS", 256),
                leaf_max_tokens=number("CHUNK_MAX_TOKENS", 512),
            ),
            retrieval=RetrievalConfig(
                route_limit=number("RETRIEVAL_ROUTE_LIMIT", 5),
                route_k=number("RETRIEVAL_ROUTE_K", 30),
                global_k=number("RETRIEVAL_GLOBAL_K", 30),
                max_candidates=number("RETRIEVAL_MAX_CANDIDATES", 60),
                token_budget=number("RETRIEVAL_TOKEN_BUDGET", 4_000),
                reference_cap=number("RETRIEVAL_REFERENCE_CAP", 5),
                keep_ratio=decimal("RETRIEVAL_KEEP_RATIO", 0.5),
            ),
            upload=UploadConfig(
                max_files_per_batch=number(
                    "UPLOAD_MAX_FILES_PER_BATCH", DEFAULT_UPLOAD_MAX_FILES_PER_BATCH
                ),
                max_file_bytes=number(
                    "UPLOAD_MAX_FILE_BYTES", DEFAULT_UPLOAD_MAX_FILE_BYTES
                ),
                max_batch_bytes=number(
                    "UPLOAD_MAX_BATCH_BYTES", DEFAULT_UPLOAD_MAX_BATCH_BYTES
                ),
                worker_count=number("UPLOAD_WORKER_COUNT", DEFAULT_UPLOAD_WORKER_COUNT),
                trash_ttl_seconds=number(
                    "UPLOAD_TRASH_TTL_SECONDS", DEFAULT_UPLOAD_TRASH_TTL_SECONDS
                ),
            ),
            logging=LoggingConfig(
                level=text("LOG_LEVEL", DEFAULT_LOG_LEVEL),
                file=optional("LOG_FILE"),
                sql=flag("LOG_SQL"),
            ),
            auth=AuthConfig(
                firebase_service_account_path=(
                    optional("FIREBASE_SERVICE_ACCOUNT_PATH")
                    or default_firebase_service_account_path()
                ),
                firebase_project_id=optional("FIREBASE_PROJECT_ID"),
                allow_x_user_id_fallback=(
                    env["AUTH_ALLOW_X_USER_ID_FALLBACK"].strip().casefold()
                    in _TRUE_VALUES
                    if "AUTH_ALLOW_X_USER_ID_FALLBACK" in env
                    else True
                ),
                admin_emails=[
                    address.strip().casefold()
                    for address in env.get("ADMIN_EMAILS", "").split(",")
                    if address.strip()
                ],
            ),
        )
