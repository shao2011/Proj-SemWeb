import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from rag_ingestion.api.routes import create_query_app
from rag_ingestion.config import ConversationConfig, RetrievalConfig
from rag_ingestion.chat import ChatTurn
from rag_ingestion.chunking import ChunkingConfig, chunk_document
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile, QdrantHybridIndex
from rag_ingestion.models import Block, BlockType, Document, OfficePathLocator, SourceType
from rag_ingestion.providers.registry import (
    Provider,
    ProviderBundle,
    ProviderRegistry,
    ProviderRequestError,
)
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.model_catalog import load_catalog
from rag_ingestion.schemas.model_catalog import ModelCatalog, ModelSpec
from rag_ingestion.schemas.understanding import MemoryCandidate, Understanding
from rag_ingestion.memory.models import (
    MEMORY_CAPS,
    MemoryOperation,
    MemoryScope,
    MemorySource,
    MemoryType,
)
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore


class FakeEmbedder:
    def embed_documents(self, items, profile, *, intent):
        return [[1.0, 0.0] for _ in items]

    def embed_query(self, text, profile, *, intent):
        return [1.0, 0.0]


class CountingReranker:
    def __init__(self) -> None:
        self.candidate_count = 0

    def rerank(self, query, documents, *, top_n):
        self.candidate_count = len(documents)
        return [
            RankedResult(index=index, relevance_score=1.0)
            for index in range(min(top_n, len(documents)))
        ]


class RecordingChat:
    model = "test-model"

    def __init__(self, parts: list[str] | None = None) -> None:
        self.parts = parts or ["Hello", " there."]
        self.calls: list[list[ChatTurn]] = []
        self.offered_tools = None
        # When set, reported through on_usage after the stream finishes, the
        # way OpenRouter's final SSE chunk carries the usage object.
        self.usage: dict[str, object] | None = None

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        model=None,
    ) -> str:
        self.calls.append(turns)
        return "".join(self.parts)

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage=None,
        tools=None,
        model=None,
        router=None,
    ) -> Iterator[str]:
        self.calls.append(turns)
        self.offered_tools = tools
        yield from self.parts
        if self.usage is not None and on_usage is not None:
            on_usage(self.usage)


class MemoryToolChat(RecordingChat):
    """Simulates a model that decides to save memory during generation."""

    def __init__(self, tool_name: str, arguments: dict, parts: list[str] | None = None) -> None:
        super().__init__(parts)
        self.tool_name = tool_name
        self.arguments = arguments
        self.tool_results: list[str] = []

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage=None,
        tools=None,
        model=None,
        router=None,
    ) -> Iterator[str]:
        self.calls.append(turns)
        self.offered_tools = tools
        # Stands in for the agent loop: find the tool the "model" picked and
        # invoke it exactly as LangChain would.
        for tool in tools or []:
            if tool.name == self.tool_name:
                self.tool_results.append(tool.invoke(self.arguments))
        yield from self.parts


class FailingChat:
    model = "test-model"

    def complete(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        model=None,
    ) -> str:
        raise ProviderRequestError("Chat provider request failed")

    def stream(
        self,
        turns: list[ChatTurn],
        *,
        max_tokens: int,
        sticky_key: str | None = None,
        on_usage=None,
        tools=None,
        model=None,
        router=None,
    ) -> Iterator[str]:
        raise ProviderRequestError("Chat provider request failed")
        yield ""


class FakeUnderstander:
    def __init__(self, understanding: Understanding) -> None:
        self.understanding = understanding
        self.calls: list[tuple[str, str]] = []

    def understand(
        self,
        query: str,
        context: str,
        *,
        sticky_key: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> Understanding:
        self.calls.append((query, context))
        self.models = getattr(self, "models", [])
        self.models.append((model, router))
        return self.understanding


def _index_evidence(qdrant, bundle, *, owner_user_id: str, project_id: str) -> None:
    document = Document.from_content(
        content=b"policy evidence",
        owner_user_id=owner_user_id,
        project_id=project_id,
        source_type=SourceType.DOCX,
        source_uri="upload://policy.docx",
        parse_engine="native",
        parse_version="1",
        title="Policy",
    )
    blocks = [
        Block.from_source(
            doc_id=document.doc_id,
            order=index,
            type=BlockType.PARAGRAPH,
            text=text,
            confidence=1.0,
            locators=[
                OfficePathLocator(
                    part="word/document.xml", path=f"/document/body/p[{index + 1}]"
                )
            ],
        )
        for index, text in enumerate(
            [
                "Article 5 defines the scope of the travel policy.",
                "Article 6 lists the travel exceptions.",
            ]
        )
    ]
    chunks = chunk_document(
        document,
        blocks,
        config=ChunkingConfig(leaf_min_tokens=1, leaf_target_tokens=4, leaf_max_tokens=8),
    )
    QdrantHybridIndex(qdrant, bundle.collection_name, bundle.profile).upsert(
        chunks.chunks, bundle.embedder
    )


def _setup(
    pg_engine,
    chat: object | None = None,
    *,
    memory: object | None = None,
    understand: object | None = None,
    summarizer: object | None = None,
    **app_options,
):
    qdrant = QdrantClient(location=":memory:")
    projects = PostgresProjectStore(pg_engine)
    threads = PostgresThreadStore(pg_engine)
    profile = EmbeddingProfile(
        provider="cohere",
        model="fake-2d",
        version="v1",
        dimensions=2,
        task="retrieval",
        mode="naive",
    )
    reranker = CountingReranker()
    registry = ProviderRegistry(
        [
            ProviderBundle(
                provider=Provider.COHERE,
                profile=profile,
                collection_name="rag-cohere-v1",
                embedder=FakeEmbedder(),
                reranker=reranker,
            )
        ]
    )
    # Conversation knobs arrive as loose kwargs so the call sites stay readable;
    # they are collected into the one config object the app now takes.
    conversation_fields = set(ConversationConfig.model_fields)
    conversation = {
        name: app_options.pop(name)
        for name in list(app_options)
        if name in conversation_fields
    }
    app = create_query_app(
        qdrant,
        registry,
        projects,
        threads=threads,
        chat=chat,
        memory=memory,
        understand=understand,
        summarizer=summarizer,
        conversation=ConversationConfig(**conversation) if conversation else None,
        **app_options,
    )
    bundle = registry.get(Provider.COHERE)
    return TestClient(app), reranker, qdrant, bundle


def _headers(user: str = "user-a") -> dict[str, str]:
    return {"X-User-ID": user}


def _events(body: str) -> list[tuple[str, dict]]:
    parsed: list[tuple[str, dict]] = []
    name: str | None = None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            parsed.append((name or "", json.loads(line[len("data: ") :])))
    return parsed


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects", json={"name": "Legal", "provider": "cohere"}, headers=_headers()
    )
    assert response.status_code == 201
    return response.json()["project_id"]


# --- thread lifecycle -------------------------------------------------------


def test_thread_without_a_project_is_created_for_plain_chat(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)

    response = client.post("/threads", json={"title": "Everyday chat"}, headers=_headers())

    assert response.status_code == 201
    body = response.json()
    assert body["project_id"] is None
    assert body["title"] == "Everyday chat"
    assert body["owner_user_id"] == "user-a"


def test_thread_defaults_its_title(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)

    response = client.post("/threads", json={}, headers=_headers())

    assert response.status_code == 201
    assert response.json()["title"] == "New chat"


def test_thread_can_be_pinned_to_an_owned_project(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    project_id = _create_project(client)

    response = client.post("/threads", json={"project_id": project_id}, headers=_headers())

    assert response.status_code == 201
    assert response.json()["project_id"] == project_id


def test_thread_cannot_be_pinned_to_someone_elses_project(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    project_id = _create_project(client)

    response = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers("user-b")
    )

    assert response.status_code == 404


def test_thread_cannot_be_pinned_to_a_missing_project(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)

    response = client.post(
        "/threads", json={"project_id": "prj_missing"}, headers=_headers()
    )

    assert response.status_code == 404


def test_listing_threads_is_owner_scoped_and_omits_messages(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, RecordingChat())
    client.post("/threads", json={"title": "Mine"}, headers=_headers())
    client.post("/threads", json={"title": "Theirs"}, headers=_headers("user-b"))

    listed = client.get("/threads", headers=_headers()).json()

    assert [item["title"] for item in listed] == ["Mine"]
    assert "messages" not in listed[0]


def test_thread_detail_returns_the_conversation(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, RecordingChat(["Answer."]))
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    detail = client.get(f"/threads/{thread_id}", headers=_headers()).json()

    assert detail["thread_id"] == thread_id
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "Hi"
    assert detail["messages"][1]["content"] == "Answer."


def test_thread_detail_is_hidden_from_other_users(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    assert client.get(f"/threads/{thread_id}", headers=_headers("user-b")).status_code == 404


def test_thread_can_be_renamed(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.patch(
        f"/threads/{thread_id}", json={"title": "Renamed"}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"
    assert (
        client.patch(
            f"/threads/{thread_id}", json={"title": "Nope"}, headers=_headers("user-b")
        ).status_code
        == 404
    )


def test_rename_cannot_move_a_thread_to_another_project(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.patch(
        f"/threads/{thread_id}",
        json={"title": "Renamed", "project_id": "prj_other"},
        headers=_headers(),
    )

    assert response.status_code == 422


def test_thread_can_be_deleted(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    assert (
        client.delete(f"/threads/{thread_id}", headers=_headers("user-b")).status_code == 404
    )
    assert client.delete(f"/threads/{thread_id}", headers=_headers()).status_code == 204
    assert client.get(f"/threads/{thread_id}", headers=_headers()).status_code == 404
    assert client.delete(f"/threads/{thread_id}", headers=_headers()).status_code == 404


# --- streaming messages -----------------------------------------------------


def test_plain_chat_streams_deltas_and_never_touches_retrieval(pg_engine) -> None:
    chat = RecordingChat(["Hel", "lo!"])
    client, reranker, _qdrant, _bundle = _setup(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [name for name, _ in events] == ["user_message", "delta", "delta", "done"]
    assert events[0][1]["role"] == "user"
    assert [payload["text"] for _, payload in events[1:3]] == ["Hel", "lo!"]
    assert events[3][1]["content"] == "Hello!"
    assert events[3][1]["citations"] == []
    assert reranker.candidate_count == 0


def test_project_thread_retrieves_evidence_and_returns_citations(pg_engine) -> None:
    chat = RecordingChat(["Article 5 sets the scope [cite:1]."])
    client, reranker, qdrant, bundle = _setup(pg_engine, chat)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "What is article 5?"},
        headers=_headers(),
    )

    events = _events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "user_message"
    assert "sources" in names
    assert names[-1] == "done"

    sources = next(payload for name, payload in events if name == "sources")
    assert sources["hits"], "retrieval returned no evidence"
    assert reranker.candidate_count > 0

    done = events[-1][1]
    assert done["content"] == "Article 5 sets the scope [cite:1]."
    # Citations are only the extracts the answer actually referenced with a
    # [cite:n] marker — not everything retrieval returned. Each one carries
    # where to look, so the document panel needs no second call and no Qdrant.
    assert done["citations"] == [
        {
            "chunk_id": sources["hits"][0]["chunk_id"],
            "doc_id": sources["hits"][0]["doc_id"],
            "block_ids": sources["hits"][0]["source_block_ids"],
        }
    ]
    assert all(citation["block_ids"] for citation in done["citations"])

    # Evidence rides on the final user turn, not the system message, so that the
    # system message stays identical across turns and can be cached.
    # The chunker splits the sentence across leaves, so match a contiguous span
    # rather than the whole indexed sentence.
    final_turn = chat.calls[0][-1].content
    assert "Article 5 defines the scope" in final_turn
    assert "## Evidence" in final_turn
    assert "Article 5 defines the scope" not in chat.calls[0][0].content
    assert "[1]" in final_turn

    reloaded = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert reloaded["messages"][-1]["citations"] == done["citations"]


def test_non_latin_text_stays_readable_on_the_stream(pg_engine) -> None:
    """Vietnamese must not arrive as \\uXXXX escapes when read through curl."""
    client, _, _qdrant, _bundle = _setup(pg_engine, RecordingChat(["Xin ch", "ào bạn!"]))
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Tiếng Việt có dấu"},
        headers=_headers(),
    )

    assert "\\u" not in response.text
    assert "Xin chào bạn!" in response.text
    assert "Tiếng Việt có dấu" in response.text
    events = _events(response.text)
    assert events[-1][1]["content"] == "Xin chào bạn!"


def test_history_is_replayed_on_the_second_turn(pg_engine) -> None:
    chat = RecordingChat(["ok"])
    client, _, _qdrant, _bundle = _setup(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "My name is Dat."},
        headers=_headers(),
    )
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "What is my name?"},
        headers=_headers(),
    )

    second_call = chat.calls[1]
    contents = [turn.content for turn in second_call]
    assert "My name is Dat." in contents
    assert contents[-1] == "What is my name?"
    assert [turn.role for turn in second_call] == ["system", "user", "assistant", "user"]


def test_provider_failure_emits_an_error_event_and_keeps_the_user_message(
    pg_engine,
) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, FailingChat())
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert [name for name, _ in events] == ["user_message", "error"]
    assert "detail" in events[1][1]

    detail = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert [item["role"] for item in detail["messages"]] == ["user"]


def test_messages_require_a_configured_chat_provider(pg_engine) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, None)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert response.status_code == 503


def test_messages_are_rejected_for_other_users_and_unknown_threads(
    pg_engine,
) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, RecordingChat())
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    assert (
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "Hi"},
            headers=_headers("user-b"),
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/threads/thr_missing/messages", json={"content": "Hi"}, headers=_headers()
        ).status_code
        == 404
    )


@pytest.mark.parametrize("content", ["", "   "])
def test_blank_message_content_is_rejected(pg_engine, content: str) -> None:
    client, _, _qdrant, _bundle = _setup(pg_engine, RecordingChat())
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": content}, headers=_headers()
    )

    assert response.status_code == 422


def test_thread_routes_are_unavailable_without_a_thread_store(pg_engine) -> None:
    qdrant = QdrantClient(location=":memory:")
    projects = PostgresProjectStore(pg_engine)
    app = create_query_app(qdrant, ProviderRegistry([]), projects)
    client = TestClient(app)

    assert client.get("/threads", headers=_headers()).status_code == 503
    assert client.post("/threads", json={}, headers=_headers()).status_code == 503


def _memory_client(pg_engine, chat) -> tuple[TestClient, PostgresMemoryStore]:
    qdrant = QdrantClient(location=":memory:")
    memory = PostgresMemoryStore(pg_engine)
    app = create_query_app(
        qdrant,
        ProviderRegistry([]),
        PostgresProjectStore(pg_engine),
        threads=PostgresThreadStore(pg_engine),
        chat=chat,
        memory=memory,
    )
    return TestClient(app), memory


def test_llm_token_usage_is_recorded_and_exposed(pg_engine) -> None:
    chat = RecordingChat()
    chat.usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
    client, _memory = _memory_client(pg_engine, chat)

    fresh = client.get("/usage", headers=_headers())
    assert fresh.status_code == 200
    assert fresh.json() == {
        "owner_user_id": "user-a",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }

    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )
    client.post(
        f"/threads/{thread_id}/messages", json={"content": "More"}, headers=_headers()
    )

    body = client.get("/usage", headers=_headers()).json()
    assert body["prompt_tokens"] == 240
    assert body["completion_tokens"] == 90
    assert body["total_tokens"] == 330
    assert body["requests"] == 2

    # Usage is scoped to the caller: another user still reads zeros.
    other = client.get("/usage", headers=_headers("user-b")).json()
    assert other["total_tokens"] == 0


def test_prompt_metrics_land_on_the_thread_after_a_turn(pg_engine) -> None:
    """The measured prompt size is what the context gauge and the summary
    trigger read; it must survive on the thread row, not just in the lifetime
    usage totals."""
    chat = RecordingChat()
    chat.usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
    client, _memory = _memory_client(pg_engine, chat)

    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    fresh = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert fresh["last_prompt_tokens"] is None
    assert fresh["context_used_percent"] is None

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    body = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert body["last_prompt_tokens"] == 120
    # No pick lands on the catalog default; the metrics must be billed to
    # the model that actually answered, not the provider's env-configured id.
    assert body["last_model_id"] == load_catalog().default
    # The chars measured must cover at least the user message itself.
    assert body["last_prompt_chars"] >= len("Hi")
    # Server-computed gauge: 120 tokens against the default model's budget
    # rounds to zero percent, but it must be present, not null.
    assert body["context_used_percent"] == 0


def test_prompt_metrics_survive_a_provider_that_reports_no_usage(pg_engine) -> None:
    chat = RecordingChat()  # .usage stays None: no on_usage callback fires
    client, _memory = _memory_client(pg_engine, chat)

    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert response.status_code == 200
    body = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert body["last_prompt_tokens"] is None


def test_the_model_catalog_is_served_for_the_picker(pg_engine) -> None:
    client, *_ = _setup(pg_engine, RecordingChat())

    body = client.get("/models").json()

    assert body["default"]
    ids = [model["id"] for model in body["models"]]
    assert body["default"] in ids
    assert all(model["label"] for model in body["models"])


def test_saved_memory_reaches_the_prompt(pg_engine) -> None:
    chat = RecordingChat()
    client, memory = _memory_client(pg_engine, chat)
    memory.add_memory(
        owner_user_id="user-a",
        scope=MemoryScope.USER,
        type=MemoryType.PREFERENCE,
        key="response_length",
        value="Two sentences at most.",
        source=MemorySource.USER_EXPLICIT,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    system = chat.calls[0][0].content
    assert "## User preferences" in system
    assert "- response_length: Two sentences at most." in system


def test_a_thread_scoped_memory_overrides_the_saved_one_end_to_end(pg_engine) -> None:
    chat = RecordingChat()
    client, memory = _memory_client(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for scope, value in (
        (MemoryScope.USER, "Vietnamese"),
        (MemoryScope.THREAD, "English"),
    ):
        memory.add_memory(
            owner_user_id="user-a",
            scope=scope,
            type=MemoryType.PREFERENCE,
            key="response_language",
            value=value,
            source=MemorySource.USER_EXPLICIT,
            thread_id=thread_id if scope is MemoryScope.THREAD else None,
        )

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    system = chat.calls[0][0].content
    assert "- response_language: English (this conversation only)" in system
    assert "Vietnamese" not in system


def test_recalled_memories_are_logged_with_their_values(pg_engine, caplog) -> None:
    """Values, not just keys.

    The rendered prompt is logged as well, so the pair is what shows a memory
    being recalled and then dropped by scope resolution or the character budget.
    A key on its own cannot show that.
    """
    chat = RecordingChat()
    client, memory = _memory_client(pg_engine, chat)
    memory.add_memory(
        owner_user_id="user-a",
        scope=MemoryScope.USER,
        type=MemoryType.FACT,
        key="occupation",
        value="Accountant at ABC",
        source=MemorySource.USER_EXPLICIT,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    with caplog.at_level("DEBUG", logger="rag_ingestion.api"):
        client.post(
            f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
        )

    recall_lines = [
        record.getMessage()
        for record in caplog.records
        if "Recalled" in record.getMessage()
    ]
    assert len(recall_lines) == 1
    assert "[user] fact occupation = Accountant at ABC" in recall_lines[0]


def test_an_empty_recall_says_so_rather_than_logging_an_empty_list(
    pg_engine, caplog
) -> None:
    chat = RecordingChat()
    client, _memory = _memory_client(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    with caplog.at_level("DEBUG", logger="rag_ingestion.api"):
        client.post(
            f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
        )

    assert any(
        "Recalled 0 memories" in record.getMessage() and "none" in record.getMessage()
        for record in caplog.records
    )


def test_llm_request_is_logged_as_one_parseable_json_object(
    pg_engine, caplog
) -> None:
    """Everything pushed to the LLM, in one block a log reader can `json.loads`.

    Model and messages are what actually goes out; `cache` is always null
    today because nothing in the OpenRouter payload sets a cache directive,
    which is the point -- it makes the always-fresh prefix visible per turn
    instead of something you have to infer.
    """
    chat = RecordingChat()
    client, _memory = _memory_client(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    with caplog.at_level("DEBUG", logger="rag_ingestion.api"):
        client.post(
            f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
        )

    request_lines = [
        record.getMessage()
        for record in caplog.records
        if "LLM request" in record.getMessage()
    ]
    assert len(request_lines) == 1
    _, _, body = request_lines[0].partition("\n")
    payload = json.loads(body)

    assert payload["model"] == load_catalog().default
    assert payload["cache"] is None
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1] == {"role": "user", "content": "Hi"}


def test_switching_memory_off_keeps_it_out_of_the_prompt(pg_engine) -> None:
    chat = RecordingChat()
    client, memory = _memory_client(pg_engine, chat)
    memory.add_memory(
        owner_user_id="user-a",
        scope=MemoryScope.USER,
        type=MemoryType.PREFERENCE,
        key="response_length",
        value="Two sentences at most.",
        source=MemorySource.USER_EXPLICIT,
    )
    memory.set_memory_enabled(owner_user_id="user-a", enabled=False)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    system = chat.calls[0][0].content
    assert "## User preferences" not in system
    assert "Two sentences at most." not in system


def test_project_thread_with_nothing_indexed_answers_as_plain_chat(pg_engine) -> None:
    """A project with no documents is an empty result, not a fault.

    The provider's collection does not exist until the first upload, and
    rejecting that with a 503 made every question in a fresh project fail. The
    thread stays pinned to the project and answers as plain chat instead, which
    is what `build_turns` already does when the evidence list is empty.
    """
    chat = RecordingChat(parts=["Chua co ", "tai lieu nao."])
    client, _reranker, qdrant, bundle = _setup(pg_engine, chat)
    project_id = _create_project(client)
    thread_id = client.post(
        "/threads",
        json={"title": "Fresh", "project_id": project_id},
        headers=_headers(),
    ).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Xin chao"},
        headers=_headers(),
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert [payload for name, payload in events if name == "sources"] == [
        {"query": "Xin chao", "hits": [], "evidence_leaf_ids": [], "routed_doc_ids": [], "diagnostics": {}}
    ]
    assert "".join(
        payload["text"] for name, payload in events if name == "delta"
    ) == "Chua co tai lieu nao."

    # Plain, not grounded: the model must not be handed an evidence block it does
    # not have, and must not be told to cite.
    system = chat.calls[0][0]
    assert system.role == "system"
    assert "Evidence:" not in system.content

    # The guard that raised the 503 also kept a read from creating the
    # collection. That property has to survive the fix.
    assert qdrant.collection_exists(bundle.collection_name) is False


# --- UnderstandMessage -------------------------------------------------------


def _candidate(
    key: str, value: str, type_: MemoryType = MemoryType.FACT
) -> MemoryCandidate:
    return MemoryCandidate(type=type_, key=key, value=value)


def _understanding(*candidates: MemoryCandidate) -> Understanding:
    return Understanding(rewritten_query=None, memory=list(candidates))


def test_model_saves_a_user_fact_through_the_memory_tool(pg_engine) -> None:
    """The ChatGPT-bio pattern: the answering model itself emits the save.

    No extra LLM call, no language-specific gating; the tool's identity fixes
    the scope, the model only chooses what is worth keeping.
    """
    chat = MemoryToolChat("save_user_memory", {"key": "nickname", "value": "Dat"})
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi, I'm Dat"}, headers=_headers()
    )

    written = memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    assert [(item.key, item.value, item.source) for item in written] == [
        ("nickname", "Dat", MemorySource.LLM_EXTRACTED)
    ]
    assert memory.list_proposals(owner_user_id="user-a") == []
    assert chat.tool_results == ["saved"]


def test_local_memory_rides_the_thread_in_plain_chat(pg_engine) -> None:
    """Without a project the local tool is conversation-scoped."""
    chat = MemoryToolChat(
        "save_conversation_memory",
        {"key": "response_length", "value": "Keep it short", "type": "preference"},
    )
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Answer briefly please"},
        headers=_headers(),
    )

    in_thread = memory.list_memories(
        owner_user_id="user-a", scope=MemoryScope.THREAD, thread_id=thread_id
    )
    assert [(item.key, item.type) for item in in_thread] == [
        ("response_length", MemoryType.PREFERENCE)
    ]
    assert memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER) == []


def test_local_memory_rides_the_project_in_project_chat(pg_engine) -> None:
    """With a project the local tool is project-scoped, and the tool offered
    to the model is named for it."""
    chat = MemoryToolChat(
        "save_project_memory",
        {"key": "other", "custom_key": "deadline", "value": "March 15"},
    )
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, qdrant, bundle = _setup(pg_engine, chat, memory=memory)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Our deadline is March 15"},
        headers=_headers(),
    )

    in_project = memory.list_memories(
        owner_user_id="user-a", scope=MemoryScope.PROJECT, project_id=project_id
    )
    assert [(item.key, item.value) for item in in_project] == [
        ("other:deadline", "March 15")
    ]
    offered = {tool.name for tool in chat.offered_tools}
    assert offered == {"save_project_memory", "save_user_memory"}


def test_restating_the_same_fact_writes_nothing_new(pg_engine) -> None:
    """Without this, every turn where the user repeats their name writes a row.

    The recall loaded for the turn doubles as the dedup set: an unchanged
    key/value comes back "already saved" and never reaches the store.
    """
    chat = MemoryToolChat("save_user_memory", {"key": "nickname", "value": "Dat"})
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for _ in range(3):
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "I'm Dat"},
            headers=_headers(),
        )

    written = memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    assert len(written) == 1
    assert [entry.op for entry in memory.audit_trail(memory_id=written[0].memory_id)] == [
        MemoryOperation.ADD
    ]
    assert chat.tool_results == ["saved", "already saved", "already saved"]


def test_a_restated_value_is_recognised_however_it_is_typed(pg_engine) -> None:
    """"Keep it short" and "keep  it short " are one instruction.

    Compared raw, a restatement superseded the live row and left an audit
    entry recording that nothing had changed.
    """
    memory = PostgresMemoryStore(pg_engine)
    saved = MemoryToolChat(
        "save_user_memory",
        {"key": "response_length", "value": "Keep it short", "type": "preference"},
    )
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, saved, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Answer briefly"},
        headers=_headers(),
    )

    restated = MemoryToolChat(
        "save_user_memory",
        {"key": "response_length", "value": "keep  it short ", "type": "preference"},
    )
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, restated, memory=memory)
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Again, briefly"},
        headers=_headers(),
    )

    live = memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    assert restated.tool_results == ["already saved"]
    assert len(live) == 1
    assert [entry.op for entry in memory.audit_trail(memory_id=live[0].memory_id)] == [
        MemoryOperation.ADD
    ]


def test_a_full_scope_evicts_the_oldest_rather_than_refusing(pg_engine) -> None:
    """The ceiling is a runaway-writer guard now, so this builds a small one.

    What it pins is the policy: a scope with no room gives up its oldest
    pipeline-written row instead of dropping the memory the user just gave.
    """
    chat = MemoryToolChat(
        "save_conversation_memory",
        {"key": "other", "custom_key": "fresh", "value": "the new one"},
    )
    memory = PostgresMemoryStore(pg_engine, caps={scope: 2 for scope in MemoryScope})
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for index in range(2):
        memory.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.THREAD,
            type=MemoryType.FACT,
            key=f"other:filler-{index}",
            value=f"Filler {index}",
            source=MemorySource.LLM_EXTRACTED,
            thread_id=thread_id,
        )

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "I'm working on project ABC"},
        headers=_headers(),
    )

    live = memory.list_memories(
        owner_user_id="user-a", scope=MemoryScope.THREAD, thread_id=thread_id
    )
    keys = {item.key for item in live}
    assert len(live) == 2
    assert "other:fresh" in keys, "the new memory wins its slot"
    assert "other:filler-0" not in keys, "the oldest makes way"
    assert chat.tool_results == ["saved"]
    assert memory.list_proposals(owner_user_id="user-a") == []


def test_a_scope_full_of_user_written_memory_proposes_instead(pg_engine) -> None:
    """Eviction spends what the pipeline wrote itself. What the user typed is
    not the pipeline's to throw away, so that case still becomes a proposal."""
    chat = MemoryToolChat(
        "save_conversation_memory",
        {"key": "other", "custom_key": "overflow", "value": "one too many"},
    )
    memory = PostgresMemoryStore(pg_engine, caps={scope: 2 for scope in MemoryScope})
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for index in range(2):
        memory.add_memory(
            owner_user_id="user-a",
            scope=MemoryScope.THREAD,
            type=MemoryType.FACT,
            key=f"other:mine-{index}",
            value=f"Mine {index}",
            source=MemorySource.USER_EXPLICIT,
            thread_id=thread_id,
        )

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "I'm working on project ABC"},
        headers=_headers(),
    )

    proposals = memory.list_proposals(owner_user_id="user-a")
    assert [(item.key, item.suggested_scope) for item in proposals] == [
        ("other:overflow", MemoryScope.THREAD)
    ]
    assert chat.tool_results == ["saved as a proposal (scope was full)"]


def test_memory_tool_skips_when_the_owner_turned_memory_off(pg_engine) -> None:
    chat = MemoryToolChat("save_user_memory", {"key": "nickname", "value": "Dat"})
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)
    memory.set_memory_enabled(owner_user_id="user-a", enabled=False)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "I'm Dat"}, headers=_headers()
    )

    assert memory.list_memories(owner_user_id="user-a") == []
    assert memory.list_proposals(owner_user_id="user-a") == []
    assert chat.tool_results == ["skipped: the user has memory turned off"]


def test_no_memory_store_means_no_tools_offered(pg_engine) -> None:
    chat = RecordingChat()
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert chat.offered_tools is None


def test_understand_rewrite_feeds_retrieval_but_never_the_answerer(pg_engine) -> None:
    chat = RecordingChat(["Article 5 sets the scope [1]."])
    understand = FakeUnderstander(
        Understanding(rewritten_query="What does Article 5 define?", memory=[])
    )
    client, reranker, qdrant, bundle = _setup(pg_engine, chat, understand=understand)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "what does it define?"},
        headers=_headers(),
    )

    events = _events(response.text)
    sources = next(payload for name, payload in events if name == "sources")
    assert sources["diagnostics"]["rewrite_used"] == 1
    assert reranker.candidate_count > 0

    # The rewrite steered retrieval; the original message is what the model
    # and the stored transcript both see.
    persisted = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert persisted["messages"][0]["content"] == "what does it define?"
    # The final turn now carries the evidence block too, so the question is a
    # substring of it rather than the whole thing. The rewrite must not appear.
    user_turn = chat.calls[0][-1]
    assert user_turn.content.endswith("what does it define?")
    assert "article 5" not in user_turn.content.lower().replace(
        "## evidence", ""
    ).split("---")[-1]

    assert understand.calls[0][0] == "what does it define?"


def test_understand_not_configured_skips_extraction_cleanly(pg_engine) -> None:
    chat = RecordingChat()
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(
        pg_engine, chat, memory=memory, understand=None
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages", json={"content": "Hi"}, headers=_headers()
    )

    assert response.status_code == 200
    assert memory.list_proposals(owner_user_id="user-a") == []
    assert (
        memory.list_memories(owner_user_id="user-a", scope=MemoryScope.THREAD, thread_id=thread_id)
        == []
    )


# --- L2 thread summary ------------------------------------------------------


class RecordingSummarizer:
    """Returns a distinct summary per call so folding is observable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[ChatTurn]]] = []

    def summarize(
        self,
        *,
        previous: str | None,
        turns: list[ChatTurn],
        sticky_key: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        router: str | None = None,
    ) -> str | None:
        self.calls.append((previous, list(turns)))
        self.models = getattr(self, "models", [])
        self.models.append((model, router))
        return f"summary#{len(self.calls)}"


def _small_window_catalog(cap: int = 6) -> ModelCatalog:
    """A catalog whose message cap is the binding window constraint."""
    return ModelCatalog(
        version=1,
        default="test/model",
        models=[
            ModelSpec(
                id="test/model",
                label="Test",
                context_tokens=1_000_000,
                max_output_tokens=1_000,
                keep_recent_max_messages=cap,
                effort=None,
            )
        ],
    )


def _mixed_visibility_catalog() -> ModelCatalog:
    """One offered model and one retired one, both still resolvable."""
    common = {
        "context_tokens": 100_000,
        "max_output_tokens": 8_000,
        "keep_recent_max_messages": 20,
        "effort": None,
    }
    return ModelCatalog(
        version=1,
        default="test/offered",
        models=[
            ModelSpec(id="test/offered", label="Offered", **common),
            ModelSpec(id="test/retired", label="Retired", enabled=False, **common),
        ],
    )


def test_the_picker_omits_disabled_models(pg_engine) -> None:
    client, *_ = _setup(pg_engine, models=_mixed_visibility_catalog())

    body = client.get("/models", headers=_headers()).json()

    assert body["default"] == "test/offered"
    assert [option["id"] for option in body["models"]] == ["test/offered"]


def _two_router_catalog() -> ModelCatalog:
    """The default on one router, a second model on another."""
    common = {
        "context_tokens": 100_000,
        "max_output_tokens": 8_000,
        "keep_recent_max_messages": 20,
        "effort": None,
    }
    return ModelCatalog(
        version=1,
        default="test/on-openrouter",
        models=[
            ModelSpec(id="test/on-openrouter", label="OpenRouter", **common),
            ModelSpec(
                id="test/on-orca", label="Orca", router="orcarouter", **common
            ),
        ],
    )


def test_the_picker_omits_models_whose_router_has_no_keys(pg_engine) -> None:
    client, *_ = _setup(
        pg_engine,
        models=_two_router_catalog(),
        configured_routers=frozenset({"openrouter"}),
    )

    body = client.get("/models", headers=_headers()).json()

    assert [option["id"] for option in body["models"]] == ["test/on-openrouter"]


def test_without_router_knowledge_the_picker_trusts_the_catalog(pg_engine) -> None:
    """Direct construction (tests, scripts) passes no router set; that must
    mean "no filtering", not "hide everything"."""
    client, *_ = _setup(pg_engine, models=_two_router_catalog())

    body = client.get("/models", headers=_headers()).json()

    assert [option["id"] for option in body["models"]] == [
        "test/on-openrouter",
        "test/on-orca",
    ]


def test_no_pick_lands_on_the_catalog_default(pg_engine) -> None:
    """The catalog is the source of truth: a turn that picked nothing runs
    the catalog default, not some env-configured model the picker never saw."""
    client, *_ = _setup(pg_engine, RecordingChat(), models=_two_router_catalog())
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Hi"},
        headers=_headers(),
    )

    done = next(p for name, p in _events(response.text) if name == "done")
    assert done["model_id"] == "test/on-openrouter"


def test_the_summary_fold_follows_the_picked_model(pg_engine) -> None:
    """Bookkeeping rides the session's model: the pre-flight fold is billed
    to the model the user is chatting with, router included."""
    common = {
        "context_tokens": 100_000,
        "max_output_tokens": 8_000,
        "keep_recent_max_messages": 6,
        "effort": None,
    }
    catalog = ModelCatalog(
        version=1,
        default="test/default",
        models=[
            ModelSpec(id="test/default", label="Default", **common),
            ModelSpec(id="test/picked", label="Picked", router="orcarouter", **common),
        ],
    )
    summarizer = RecordingSummarizer()
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        summarizer=summarizer,
        models=catalog,
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(10):
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": f"message {index}", "model_id": "test/picked"},
            headers=_headers(),
        )

    assert summarizer.calls, "the window never overflowed; the test drives nothing"
    assert set(summarizer.models) == {("test/picked", "orcarouter")}


def test_a_model_on_a_keyless_router_degrades_to_the_default(pg_engine) -> None:
    """A stale picker cache may still request the hidden model; the turn
    answers on the default instead of failing, logged dev-side only."""
    chat = RecordingChat()
    client, *_ = _setup(
        pg_engine,
        chat,
        models=_two_router_catalog(),
        configured_routers=frozenset({"openrouter"}),
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Hi", "model_id": "test/on-orca"},
        headers=_headers(),
    )

    assert response.status_code == 200
    events = _events(response.text)
    done = next(payload for name, payload in events if name == "done")
    assert done["model_id"] == "test/on-openrouter"


def _say(client, thread_id: str, text: str) -> None:
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": text},
        headers=_headers(),
    )


def test_a_short_thread_is_never_summarized(pg_engine) -> None:
    summarizer = RecordingSummarizer()
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        summarizer=summarizer,
        models=_small_window_catalog(cap=6),
        history_keep_recent=4,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(3):
        _say(client, thread_id, f"message {index}")

    assert summarizer.calls == []
    thread = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert thread["summary"] is None


def test_a_long_thread_summarizes_the_middle_and_sets_the_watermark(
    pg_engine,
) -> None:
    summarizer = RecordingSummarizer()
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        summarizer=summarizer,
        models=_small_window_catalog(cap=6),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    # Each send stores a user message and an assistant reply. The fifth send
    # sees eight prior messages: the cap of six squeezes positions 2-4 out of
    # the window (fold runs pre-flight, before the fifth answer streams), and
    # the refill share cuts to four kept, so the watermark lands at 4.
    for index in range(5):
        _say(client, thread_id, f"message {index}")

    assert summarizer.calls, "a thread this long should have been summarized"
    thread = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert thread["summary"] is not None
    assert thread["summarized_through_position"] == 4


def test_summarizing_is_incremental_not_a_rewrite_of_the_whole_middle(
    pg_engine,
) -> None:
    """The point of the watermark.

    A second summarization must be handed the previous summary plus only the
    turns that newly dropped out, never the whole middle again.
    """
    summarizer = RecordingSummarizer()
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        summarizer=summarizer,
        models=_small_window_catalog(cap=6),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(7):
        _say(client, thread_id, f"message {index}")

    assert len(summarizer.calls) >= 2
    first_previous, first_turns = summarizer.calls[0]
    second_previous, second_turns = summarizer.calls[1]
    assert first_previous is None
    assert second_previous == "summary#1"
    # No turn is summarized twice. Compared on the user messages, which are
    # unique per send; the assistant's reply is a fixed string in this fake and
    # would collide across batches for reasons that have nothing to do with the
    # watermark.
    def _user_texts(turns: list[ChatTurn]) -> set[str]:
        return {turn.content for turn in turns if turn.role == "user"}

    assert _user_texts(first_turns) & _user_texts(second_turns) == set()


def test_the_summary_reaches_the_prompt_of_the_turn_that_folded(pg_engine) -> None:
    """Pre-flight folding: the summary lands in the same prompt whose window
    squeezed those turns out, not one turn later."""
    chat = RecordingChat()
    summarizer = RecordingSummarizer()
    client, *_ = _setup(
        pg_engine,
        chat,
        summarizer=summarizer,
        models=_small_window_catalog(cap=6),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(5):
        _say(client, thread_id, f"message {index}")

    system = chat.calls[-1][0].content
    assert "## Earlier in this conversation" in system
    assert "summary#" in system


def test_dropped_turns_leave_the_replayed_history(pg_engine) -> None:
    """The other half of summarizing: the middle stops being replayed."""
    chat = RecordingChat()
    client, *_ = _setup(
        pg_engine,
        chat,
        summarizer=RecordingSummarizer(),
        models=_small_window_catalog(cap=6),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(5):
        _say(client, thread_id, f"message {index}")
    _say(client, thread_id, "latest")

    replayed = [turn.content for turn in chat.calls[-1][1:-1]]
    assert "message 0" in replayed, "the opening turn is kept"
    assert "message 1" not in replayed, "the middle is dropped"
    assert "message 4" in replayed, "recent turns are kept"


def test_no_summarizer_configured_leaves_threads_untouched(pg_engine) -> None:
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        summarizer=None,
        models=_small_window_catalog(cap=6),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(5):
        _say(client, thread_id, f"message {index}")

    thread = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert thread["summary"] is None
    assert thread["summarized_through_position"] is None


# --- what UnderstandMessage is shown ----------------------------------------


def test_understand_sees_only_the_configured_number_of_recent_messages(
    pg_engine,
) -> None:
    """Four messages, not six, and the cap is the config value.

    The slice counts messages rather than pairs, so the number is even on
    purpose: an odd cap would hand the model an answer whose question had been
    cut away.
    """
    understand = FakeUnderstander(Understanding(rewritten_query=None, memory=[]))
    client, *_ = _setup(pg_engine, RecordingChat(), understand=understand)
    project_id = _create_project(client)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    for index in range(3):
        _say(client, thread_id, f"message {index}")
    _say(client, thread_id, "the latest question")

    context = understand.calls[-1][1]
    assert "message 0" not in context, "the oldest pair is outside the 4-message window"
    assert "message 2" in context, "the most recent pair is inside it"


def test_understand_is_given_the_thread_summary_when_there_is_one(pg_engine) -> None:
    """The summary is the only thing that can resolve a reference to a turn
    that has already scrolled out of the small raw window."""
    understand = FakeUnderstander(Understanding(rewritten_query=None, memory=[]))
    client, *_ = _setup(pg_engine, RecordingChat(), understand=understand)
    threads = PostgresThreadStore(pg_engine)
    project_id = _create_project(client)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]
    threads.set_summary(
        owner_user_id="user-a",
        thread_id=thread_id,
        summary="The user introduced themselves as an accountant at ABC.",
        summarized_through_position=1,
    )

    _say(client, thread_id, "where do I work again?")

    context = understand.calls[-1][1]
    assert "accountant at ABC" in context
    assert "may be lossy" in context, (
        "generated prose must be labelled so it is not read as a quote"
    )


def test_a_long_summary_cannot_evict_the_recent_turns(pg_engine) -> None:
    """Separate budgets, not one shared pool.

    A shared budget would let a long summary push out the most recent turns,
    which are the stronger signal for resolving a reference.
    """
    understand = FakeUnderstander(Understanding(rewritten_query=None, memory=[]))
    client, *_ = _setup(
        pg_engine,
        RecordingChat(),
        understand=understand,
        understand_summary_chars=50,
    )
    threads = PostgresThreadStore(pg_engine)
    project_id = _create_project(client)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]
    threads.set_summary(
        owner_user_id="user-a",
        thread_id=thread_id,
        summary="x" * 5_000,
        summarized_through_position=1,
    )

    _say(client, thread_id, "a distinctive recent question")
    _say(client, thread_id, "the latest question")

    context = understand.calls[-1][1]
    assert "a distinctive recent question" in context
    assert context.count("x") <= 50, "the summary is held to its own budget"


def test_no_summary_means_no_summary_section(pg_engine) -> None:
    understand = FakeUnderstander(Understanding(rewritten_query=None, memory=[]))
    client, *_ = _setup(pg_engine, RecordingChat(), understand=understand)
    project_id = _create_project(client)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    _say(client, thread_id, "a question with no history behind it")

    context = understand.calls[-1][1]
    assert "may be lossy" not in context


def test_retrieval_config_reaches_the_chain(pg_engine) -> None:
    """Proof the wiring is live, not just that the setting parses.

    `create_query_app` accepted `retrieval_config` all along, but nothing ever
    passed one, so production ran the defaults no matter what was configured.
    The reranker sees the candidate pool, so its size is the observable end of
    that wire.
    """
    chat = RecordingChat(["ok"])
    client, reranker, qdrant, bundle = _setup(
        pg_engine, chat, retrieval_config=RetrievalConfig(max_candidates=1)
    )
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "what is article 5?"},
        headers=_headers(),
    )

    assert reranker.candidate_count == 1, (
        "the configured candidate budget did not reach run_retrieval"
    )


def test_the_default_candidate_budget_is_much_larger(pg_engine) -> None:
    """Negative control: without the override the pool is not capped at 1, so
    the test above is measuring the setting rather than a coincidence."""
    chat = RecordingChat(["ok"])
    client, reranker, qdrant, bundle = _setup(pg_engine, chat)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "what is article 5?"},
        headers=_headers(),
    )

    assert reranker.candidate_count > 1


def test_merged_section_reaches_the_prompt_as_one_block(pg_engine) -> None:
    chat = RecordingChat(["Both articles say travel [cite:1]."])
    client, _, qdrant, bundle = _setup(pg_engine, chat)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "travel rules?"},
        headers=_headers(),
    )
    events = _events(response.text)

    sources = next(payload for name, payload in events if name == "sources")
    assert len(sources["hits"]) >= 2, "sanity: several sibling leaves recalled"

    # The prompt gets the merged section: one evidence block holding both
    # sentences in document order, not one numbered fragment per leaf. The
    # tiny test chunker splits mid-sentence, so match contiguous spans.
    final_turn = chat.calls[0][-1].content
    assert "Article 5 defines the scope of the travel" in final_turn
    assert "Article 6 lists the travel exceptions." in final_turn
    assert "[2]" not in final_turn

    # Citations stay leaf-level even though the prompt saw the parent, and only
    # the extract the answer referenced comes back.
    done = events[-1][1]
    assert done["citations"] == [
        {
            "chunk_id": sources["hits"][0]["chunk_id"],
            "doc_id": sources["hits"][0]["doc_id"],
            "block_ids": sources["hits"][0]["source_block_ids"],
        }
    ]


# --- deferred understanding for plain chat ----------------------------------


def _order_fakes(order: list[str], understanding=None):
    class OrderChat(RecordingChat):
        def stream(self, turns, *, max_tokens, sticky_key=None, on_usage=None, tools=None, model=None, router=None):
            order.append("stream")
            yield from super().stream(turns, max_tokens=max_tokens, sticky_key=sticky_key)

    class OrderUnderstander(FakeUnderstander):
        def understand(self, query, context, *, sticky_key=None, model=None, router=None):
            order.append("understand")
            return super().understand(query, context, sticky_key=sticky_key)

    return OrderChat(), OrderUnderstander(
        understanding or Understanding(rewritten_query=None, memory=[])
    )


def test_plain_chat_never_calls_understand(pg_engine) -> None:
    order: list[str] = []
    chat, understander = _order_fakes(order)
    client, _, _qdrant, _bundle = _setup(pg_engine, chat, understand=understander)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Cho danh sách 5 món ăn sáng phổ biến"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert order == ["stream"], (
        "plain chat has no retrieval to steer and saves memory through tools, "
        "so the understand LLM call must not run at all"
    )


def test_project_chat_still_understands_before_streaming(pg_engine) -> None:
    order: list[str] = []
    chat, understander = _order_fakes(order)
    client, _, qdrant, bundle = _setup(pg_engine, chat, understand=understander)
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)
    thread_id = client.post(
        "/threads", json={"project_id": project_id}, headers=_headers()
    ).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "travel rules?"},
        headers=_headers(),
    )

    assert order == ["understand", "stream"], (
        "the rewrite feeds retrieval, so project chat keeps understand first"
    )



def test_a_local_memory_repeated_across_threads_is_promoted_to_user_scope(
    pg_engine,
) -> None:
    """Design 6.5: the same key written in enough separate conversations is a
    habit, not a request about one of them.

    Promotion adds a user-scoped row rather than moving any thread's own, so
    each conversation keeps the memory it earned.
    """
    chat = MemoryToolChat(
        "save_conversation_memory",
        {"key": "response_length", "value": "Keep it short", "type": "preference"},
    )
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(
        pg_engine, chat, memory=memory, promotion_thread_threshold=3
    )

    thread_ids = []
    for _ in range(3):
        thread_id = client.post("/threads", json={}, headers=_headers()).json()[
            "thread_id"
        ]
        thread_ids.append(thread_id)
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "Answer briefly please"},
            headers=_headers(),
        )

    promoted = memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER)
    assert [(item.key, item.value) for item in promoted] == [
        ("response_length", "Keep it short")
    ]
    assert (
        len(memory.list_memories(owner_user_id="user-a", scope=MemoryScope.THREAD)) == 3
    )


def test_two_threads_are_not_enough_to_promote(pg_engine) -> None:
    """The threshold is five: one repeat is coincidence, not a habit."""
    chat = MemoryToolChat(
        "save_conversation_memory",
        {"key": "response_length", "value": "Keep it short", "type": "preference"},
    )
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, _qdrant, _bundle = _setup(pg_engine, chat, memory=memory)

    for _ in range(2):
        thread_id = client.post("/threads", json={}, headers=_headers()).json()[
            "thread_id"
        ]
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "Answer briefly please"},
            headers=_headers(),
        )

    assert memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER) == []


def test_project_scoped_memory_is_never_promoted(pg_engine) -> None:
    """A project memory is about the project, however often it recurs."""
    chat = MemoryToolChat(
        "save_project_memory",
        {"key": "other", "custom_key": "deadline", "value": "March 15"},
    )
    memory = PostgresMemoryStore(pg_engine)
    client, _reranker, qdrant, bundle = _setup(
        pg_engine, chat, memory=memory, promotion_thread_threshold=2
    )
    project_id = _create_project(client)
    _index_evidence(qdrant, bundle, owner_user_id="user-a", project_id=project_id)

    for _ in range(3):
        thread_id = client.post(
            "/threads", json={"project_id": project_id}, headers=_headers()
        ).json()["thread_id"]
        client.post(
            f"/threads/{thread_id}/messages",
            json={"content": "Our deadline is March 15"},
            headers=_headers(),
        )

    assert memory.list_memories(owner_user_id="user-a", scope=MemoryScope.USER) == []

