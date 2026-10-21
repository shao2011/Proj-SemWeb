"""The budget-driven history window and the pre-flight summary fold.

The window is sized to the model: the opening turn plus the newest messages
that fit the spec's message cap and char budget. Whatever falls out of the
window and is not yet under the summary watermark is folded into the summary
*before* generation -- the prompt that goes out never silently misses turns
that nothing carries.

Folds amortize: a fold cuts the window to a refill share of its cap, so the
freed headroom absorbs the next several turns and folding does not run on
every message.

The tests drive a small message cap rather than a real 1M-token budget: the
cap is a catalog parameter, and the behavior is about crossing it, not about
its value.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from rag_ingestion.api.app import create_query_app
from rag_ingestion.chat import ChatTurn
from rag_ingestion.config import ConversationConfig
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.retrieval.reranking import RankedResult
from rag_ingestion.schemas.model_catalog import ModelCatalog, ModelSpec
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.summary import parse_items
from fastapi.testclient import TestClient


class _Chat:
    model = "test-model"

    def __init__(self) -> None:
        self.turns: list[list[ChatTurn]] = []
        self.max_tokens: list[int] = []
        self.models: list[str | None] = []
        #: Set to make the fake end its reply with a used-marker, the way the
        #: answering prompt asks a real model to.
        self.marker: str | None = None
        #: Set to make the fake report a prompt size, the way a real provider
        #: does through the usage callback. None keeps usage silent.
        self.usage_prompt_tokens: int | None = None

    def complete(
        self, turns, *, max_tokens, sticky_key=None, model=None
    ) -> str:
        self.turns.append(list(turns))
        return "ok"

    def stream(
        self,
        turns,
        *,
        max_tokens,
        sticky_key=None,
        on_usage=None,
        tools=None,
        model=None,
        router=None,
    ) -> Iterator[str]:
        self.turns.append(list(turns))
        self.max_tokens.append(max_tokens)
        self.models.append(model)
        if on_usage is not None and self.usage_prompt_tokens is not None:
            on_usage({"prompt_tokens": self.usage_prompt_tokens})
        yield "ok"
        if self.marker is not None:
            # Split across chunks the way real tokens arrive.
            yield self.marker[:5]
            yield self.marker[5:]


class _Summarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[ChatTurn]]] = []
        self.instructions: list[str | None] = []
        #: Set to make every fold come back empty, the way a provider outage
        #: or a non-JSON reply does.
        self.fail = False

    def summarize(
        self, *, previous, turns, sticky_key=None, instructions=None, model=None, router=None
    ) -> str | None:
        if self.fail:
            return None
        self.calls.append((previous, list(turns)))
        self.instructions.append(instructions)
        return f"summary#{len(self.calls)}"


class _Embedder:
    def embed_documents(self, items, profile, *, intent):
        return [[0.0, 0.0] for _ in items]

    def embed_query(self, text, profile, *, intent):
        return [0.0, 0.0]


class _Reranker:
    def rerank(self, query, documents, *, top_n):
        return [
            RankedResult(index=index, relevance_score=1.0)
            for index in range(min(top_n, len(documents)))
        ]


def _headers(user: str = "user-a") -> dict[str, str]:
    return {"X-User-ID": user}


def _catalog(
    cap: int = 6, context_tokens: int = 1_000_000, max_output_tokens: int = 1_000
) -> ModelCatalog:
    """A one-model catalog whose message cap is the binding constraint."""
    return ModelCatalog(
        version=1,
        default="test/model",
        models=[
            ModelSpec(
                id="test/model",
                label="Test",
                context_tokens=context_tokens,
                max_output_tokens=max_output_tokens,
                keep_recent_max_messages=cap,
                effort=None,
            )
        ],
    )


@pytest.fixture
def harness(pg_engine):
    """A chat app with a controllable model catalog and window knobs."""

    def build(*, models: ModelCatalog | None = None, **conversation):
        threads = PostgresThreadStore(pg_engine)
        chat = _Chat()
        summarizer = _Summarizer()
        profile = EmbeddingProfile(
            provider="cohere",
            model="fake-2d",
            version="v1",
            dimensions=2,
            task="retrieval",
            mode="naive",
        )
        app = create_query_app(
            QdrantClient(location=":memory:"),
            ProviderRegistry(
                [
                    ProviderBundle(
                        provider=Provider.COHERE,
                        profile=profile,
                        collection_name="rag-cohere-v1",
                        embedder=_Embedder(),
                        reranker=_Reranker(),
                    )
                ]
            ),
            PostgresProjectStore(pg_engine),
            threads=threads,
            chat=chat,
            summarizer=summarizer,
            models=models or _catalog(),
            conversation=ConversationConfig(**conversation),
        )
        return TestClient(app), threads, chat, summarizer

    return build


def _say(client: TestClient, thread_id: str, text: str) -> None:
    client.post(
        f"/threads/{thread_id}/messages", json={"content": text}, headers=_headers()
    )


def _narrow(threads: PostgresThreadStore, limit: int) -> None:
    """Shrink the read window, which is what a 200-message thread does in the large."""
    original = threads.list_messages

    def capped(*, owner_user_id: str, thread_id: str, limit: int = limit):
        return original(
            owner_user_id=owner_user_id, thread_id=thread_id, limit=limit
        )

    threads.list_messages = capped  # type: ignore[method-assign]


def test_the_prompt_replays_the_recent_turns_not_the_first_ones(harness) -> None:
    """The heart of it: a capped window must hold the present.

    Ordered ascending under a LIMIT, the window held the *oldest* messages, so
    every turn past the cap was answered against the opening of the conversation
    and the turns immediately before the question were the ones dropped.
    """
    client, threads, chat, _ = harness(
        models=_catalog(cap=6), history_keep_recent=4
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    _narrow(threads, 6)

    for index in range(8):
        _say(client, thread_id, f"message {index}")

    replayed = [turn.content for turn in chat.turns[-1] if turn.role == "user"]
    assert "message 7" in replayed[-1]
    assert "message 6" in " ".join(replayed)
    # The opening turn is deliberately still there, it is where the subject of
    # the thread is established, and the window keeps it on purpose.
    assert "message 0" in " ".join(replayed)
    # But the run of early turns after the opener must be gone, not the recent ones.
    assert "message 1" not in " ".join(replayed)


def test_folding_happens_before_generation_not_after(harness) -> None:
    """Pre-flight: the summary produced by a fold must reach the very prompt
    whose window squeezed those turns out -- not the next one."""
    client, _, chat, summarizer = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(5):
        _say(client, thread_id, f"message {index}")

    assert summarizer.calls, "the window overflowed; a fold was due"
    system = chat.turns[-1][0].content
    assert "summary#1" in system, "the fold's output must ride the same turn"


def test_folds_amortize_instead_of_running_every_turn(harness) -> None:
    """The refill share frees headroom, so folding skips turns.

    Without it, once a thread crosses its cap every send pays a summarizer
    call on the critical path -- the fold-every-turn bug, moved pre-flight.
    """
    client, _, _, summarizer = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(9):
        _say(client, thread_id, f"message {index}")

    # Nine sends with cap 6 overflow repeatedly; amortization keeps the fold
    # count well under one per send.
    assert 1 <= len(summarizer.calls) <= 4


def test_the_summary_watermark_keeps_advancing_past_the_read_limit(harness) -> None:
    """It used to freeze at `limit - history_keep_recent` and never move again.

    The fold span is computed from real message positions, so a read window
    that saturates must not freeze the watermark with it.
    """
    client, threads, _, summarizer = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    _narrow(threads, 6)

    watermarks: list[int | None] = []
    for index in range(9):
        _say(client, thread_id, f"message {index}")
        watermarks.append(
            client.get(f"/threads/{thread_id}", headers=_headers()).json()[
                "summarized_through_position"
            ]
        )

    assert summarizer.calls
    # The watermark must sail past the read limit, where the old arithmetic
    # froze it for the life of the thread.
    assert watermarks[-1] > 6 - 2
    # Monotonic: a watermark that went backwards would re-fold turns already in
    # the summary.
    seen = [value for value in watermarks if value is not None]
    assert seen == sorted(seen)


def test_no_turn_is_folded_into_the_summary_twice(harness) -> None:
    client, threads, _, summarizer = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    _narrow(threads, 6)

    for index in range(9):
        _say(client, thread_id, f"message {index}")

    folded = [
        turn.content
        for _, turns in summarizer.calls
        for turn in turns
        if turn.role == "user"
    ]
    assert folded, "nine sends over a cap of six must fold something"
    assert len(folded) == len(set(folded)), folded


def test_one_fold_stays_bounded_when_the_watermark_is_far_behind(harness) -> None:
    """A backlog must not be handed to the summarizer in one call.

    Reachable without a fault: the summarizer is optional, so a thread that ran
    for a long time without one has its whole middle waiting the moment one is
    configured.
    """
    client, threads, _, summarizer = harness(
        models=_catalog(cap=6),
        history_keep_recent=2,
        summary_max_fold_messages=3,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(8):
        _say(client, thread_id, f"message {index}")

    assert summarizer.calls
    assert all(len(turns) <= 3 for _, turns in summarizer.calls)
    # Capped, not abandoned: the watermark still moved.
    thread = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert thread["summarized_through_position"] >= 3


def test_a_char_budget_squeezes_the_window_before_the_message_cap(harness) -> None:
    """The second dimension: long messages must shrink the window even when
    the message count is nowhere near the cap."""
    # usable = 4000 - 400 - safety floor 2000 = 1600 tokens; at the default
    # calibration that is ~4000 chars of whole-prompt budget, so a handful of
    # ~200-char messages overflows history long before the cap of 40.
    client, _, chat, summarizer = harness(
        models=_catalog(cap=40, context_tokens=4_000, max_output_tokens=400),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    filler = "x" * 200
    for index in range(12):
        _say(client, thread_id, f"message {index} {filler}")

    assert summarizer.calls, "long turns must overflow the char budget"
    replayed = " ".join(turn.content for turn in chat.turns[-1])
    assert "message 11" in replayed, "the newest turn always survives"
    assert "message 1 " not in replayed, "early turns fell to the char budget"


def test_the_generation_cap_comes_from_the_model_spec(harness) -> None:
    client, _, chat, _ = harness(
        models=_catalog(cap=6, max_output_tokens=777), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    _say(client, thread_id, "hello")

    assert chat.max_tokens[-1] == 777


def test_a_requested_model_is_passed_through_to_the_provider(harness) -> None:
    client, _, chat, _ = harness(models=_catalog(cap=6), history_keep_recent=2)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello", "model_id": "test/model"},
        headers=_headers(),
    )
    _say(client, thread_id, "no model this time")

    # Explicit pick rides through; no pick lands on the catalog default,
    # which is the source of truth now that models live on different routers.
    assert chat.models[-2] == "test/model"
    assert chat.models[-1] == "test/model"


def test_manual_compact_folds_down_to_the_floor(harness) -> None:
    """The gauge's click-to-compact: an explicit request folds everything
    outside the floor window, without waiting for the budget to force it."""
    client, _, _, summarizer = harness(
        models=_catalog(cap=40), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(6):
        _say(client, thread_id, f"message {index}")
    assert summarizer.calls == [], "cap 40 leaves nothing to fold on its own"

    response = client.post(f"/threads/{thread_id}/compact", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    # Twelve messages, floor two: everything from position 2 through 10 folds.
    assert body["summarized_through_position"] == 10
    assert summarizer.calls


def test_manual_compact_on_a_short_thread_reports_nothing_to_do(harness) -> None:
    client, _, _, summarizer = harness(models=_catalog(cap=40))
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    _say(client, thread_id, "only message")

    response = client.post(f"/threads/{thread_id}/compact", headers=_headers())

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert summarizer.calls == []


def test_manual_compact_works_off_a_deep_backlog_in_bounded_folds(harness) -> None:
    client, _, _, summarizer = harness(
        models=_catalog(cap=40),
        history_keep_recent=2,
        summary_max_fold_messages=3,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(6):
        _say(client, thread_id, f"message {index}")

    response = client.post(f"/threads/{thread_id}/compact", headers=_headers())

    assert response.status_code == 200
    assert response.json()["summarized_through_position"] == 10
    # Several bounded calls rather than one giant one.
    assert len(summarizer.calls) >= 3
    assert all(len(turns) <= 3 for _, turns in summarizer.calls)


def test_manual_compact_lowers_the_gauge(harness) -> None:
    """The design says the percent updates from the compact response
    (IMPLEMENT.md). It can only do that if compacting re-estimates the
    prompt size instead of echoing the last measured one."""
    client, _, chat, _ = harness(
        models=_catalog(cap=40, context_tokens=103_000),
        history_keep_recent=2,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    chat.usage_prompt_tokens = 50_000  # 50% of the 100k usable budget
    for index in range(6):
        _say(client, thread_id, f"message {index}")

    before = client.get(f"/threads/{thread_id}", headers=_headers()).json()[
        "context_used_percent"
    ]
    assert before == 50

    body = client.post(f"/threads/{thread_id}/compact", headers=_headers()).json()

    assert body["changed"] is True
    assert body["context_used_percent"] is not None
    assert body["context_used_percent"] < before
    # And the estimate sticks: the thread view reads it back.
    after = client.get(f"/threads/{thread_id}", headers=_headers()).json()[
        "context_used_percent"
    ]
    assert after == body["context_used_percent"]


def test_manual_compact_reports_a_stall_when_the_summarizer_fails(harness) -> None:
    """A failed fold must not masquerade as 'nothing to compact': the client
    needs to tell the user the click did not work."""
    client, _, _, summarizer = harness(
        models=_catalog(cap=40), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(6):
        _say(client, thread_id, f"message {index}")
    summarizer.fail = True

    body = client.post(f"/threads/{thread_id}/compact", headers=_headers()).json()

    assert body["changed"] is False
    assert body["stalled"] is True


def test_manual_compact_on_a_short_thread_is_not_stalled(harness) -> None:
    client, _, _, _ = harness(models=_catalog(cap=40))
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    _say(client, thread_id, "only message")

    body = client.post(f"/threads/{thread_id}/compact", headers=_headers()).json()

    assert body["changed"] is False
    assert body["stalled"] is False


def test_folds_respect_the_char_cap_not_just_the_message_cap(harness) -> None:
    """A fold of 40 RAG-sized answers can dwarf the summarizer's own context.
    The char cap is the binding one; the message cap only bounds the fetch."""
    client, _, _, summarizer = harness(
        models=_catalog(cap=40),
        history_keep_recent=2,
        summary_max_fold_messages=40,
        summary_max_fold_chars=30,
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(6):
        _say(client, thread_id, f"message {index}")

    response = client.post(f"/threads/{thread_id}/compact", headers=_headers())

    assert response.status_code == 200
    # The whole backlog still gets worked off, in char-bounded folds.
    assert response.json()["summarized_through_position"] == 10
    assert len(summarizer.calls) >= 2
    for _, turns in summarizer.calls:
        total = sum(len(turn.content) for turn in turns)
        # A single message may exceed the cap alone: it must still fold,
        # or the watermark stalls forever.
        assert total <= 30 or len(turns) == 1


def test_manual_compact_passes_instructions_to_the_summarizer(harness) -> None:
    client, _, _, summarizer = harness(
        models=_catalog(cap=40), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(6):
        _say(client, thread_id, f"message {index}")

    response = client.post(
        f"/threads/{thread_id}/compact",
        json={"instructions": "focus on the deadlines"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert summarizer.instructions
    assert all(
        item == "focus on the deadlines" for item in summarizer.instructions
    )


def test_the_used_marker_never_reaches_the_client_or_the_stored_message(
    harness,
) -> None:
    """End to end: the marker is an internal channel. It must not show up in
    the SSE deltas the browser renders, nor in the message row that later
    turns replay as history."""
    client, threads, chat, _ = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for index in range(5):
        _say(client, thread_id, f"message {index}")
    assert threads.get_thread(owner_user_id="user-a", thread_id=thread_id).summary

    chat.marker = "\n<!--used:s1-->"
    with client.stream(
        "POST",
        f"/threads/{thread_id}/messages",
        json={"content": "and now?"},
        headers=_headers(),
    ) as response:
        body = "".join(response.iter_text())

    assert "ok" in body, "the answer itself still streams"
    assert "<!--used" not in body, "the marker must not ride the SSE stream"
    detail = client.get(f"/threads/{thread_id}", headers=_headers()).json()
    assert all("<!--" not in item["content"] for item in detail["messages"])


def test_a_used_marker_credits_the_item_it_names(harness) -> None:
    client, threads, chat, _ = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for index in range(5):
        _say(client, thread_id, f"message {index}")

    chat.marker = "<!--used:s1-->"
    _say(client, thread_id, "which item did you use?")

    stored = threads.get_thread(owner_user_id="user-a", thread_id=thread_id).summary
    credited = {item.id: item.used for item in parse_items(stored)}
    assert credited.get("s1", 0) >= 1, stored


def test_a_marker_naming_an_unknown_item_credits_nothing(harness) -> None:
    client, threads, chat, _ = harness(
        models=_catalog(cap=6), history_keep_recent=2
    )
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    for index in range(5):
        _say(client, thread_id, f"message {index}")

    chat.marker = "<!--used:s99-->"
    _say(client, thread_id, "hallucinated id")

    stored = threads.get_thread(owner_user_id="user-a", thread_id=thread_id).summary
    assert all(item.used == 0 for item in parse_items(stored))


def test_the_thread_read_endpoint_reports_where_its_slice_sits(harness) -> None:
    """A client shown a partial thread has to be able to tell that it is partial."""
    client, threads, _, _ = harness(models=_catalog(cap=6))
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    for index in range(3):
        _say(client, thread_id, f"message {index}")

    body = client.get(f"/threads/{thread_id}?limit=2", headers=_headers()).json()

    assert [item["position"] for item in body["messages"]] == [5, 6]
