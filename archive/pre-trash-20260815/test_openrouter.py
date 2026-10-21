import json
import logging

import httpx
import pytest

from rag_ingestion.chat import ChatCompleter, ChatTurn
from rag_ingestion.providers.openrouter import OpenRouterChat
from rag_ingestion.providers.registry import (
    ProviderRequestError,
    ProviderResponseError,
)


TURNS = [
    ChatTurn(role="system", content="You are helpful."),
    ChatTurn(role="user", content="What is article 5?"),
]


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://openrouter.ai",
        transport=httpx.MockTransport(handler),
    )


def _sse(*chunks: str) -> bytes:
    return "".join(f"{chunk}\n\n" for chunk in chunks).encode()


def _delta(text: str) -> str:
    return f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}'


def test_adapter_satisfies_the_protocol() -> None:
    adapter = OpenRouterChat(["key"], "anthropic/claude-opus-5", client=_client(lambda _: httpx.Response(200)))

    assert isinstance(adapter, ChatCompleter)


def test_complete_posts_the_documented_request_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Article 5 defines scope."}}]}
        )

    adapter = OpenRouterChat(["secret-key"], "anthropic/claude-opus-5", client=_client(handler))
    answer = adapter.complete(TURNS, max_tokens=512)

    assert answer == "Article 5 defines scope."
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"] == {
        "model": "anthropic/claude-opus-5",
        "max_tokens": 512,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is article 5?"},
        ],
    }


def test_complete_sanitizes_provider_http_errors() -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(lambda _: httpx.Response(401, json={"error": "bad key"})),
    )

    with pytest.raises(ProviderRequestError, match="Chat provider request failed"):
        adapter.complete(TURNS, max_tokens=16)


def test_complete_rejects_a_response_without_choices() -> None:
    adapter = OpenRouterChat(["key"], "model", client=_client(lambda _: httpx.Response(200, json={"choices": []}))
    )

    with pytest.raises(ProviderResponseError):
        adapter.complete(TURNS, max_tokens=16)


def test_complete_rejects_a_non_json_body() -> None:
    adapter = OpenRouterChat(["key"], "model", client=_client(lambda _: httpx.Response(200, content=b"<html>"))
    )

    with pytest.raises(ProviderResponseError):
        adapter.complete(TURNS, max_tokens=16)


def test_complete_rejects_blank_generated_text() -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(
            lambda _: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})
        ),
    )

    with pytest.raises(ProviderResponseError):
        adapter.complete(TURNS, max_tokens=16)


def test_complete_logs_usage_when_present(caplog: pytest.LogCaptureFixture) -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Article 5 defines scope."}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                },
            )
        ),
    )

    with caplog.at_level(logging.INFO, logger="rag_ingestion.providers.openrouter"):
        adapter.complete(TURNS, max_tokens=16)

    assert "cached_tokens" in caplog.text
    assert "120" in caplog.text


def test_complete_does_not_log_usage_when_absent(caplog: pytest.LogCaptureFixture) -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(
            lambda _: httpx.Response(
                200, json={"choices": [{"message": {"content": "Article 5."}}]}
            )
        ),
    )

    with caplog.at_level(logging.INFO, logger="rag_ingestion.providers.openrouter"):
        adapter.complete(TURNS, max_tokens=16)

    assert "usage" not in caplog.text.lower()


def test_stream_yields_deltas_in_order_and_requests_streaming() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                ": OPENROUTER PROCESSING",
                _delta("Article "),
                _delta("5 "),
                'data: {"choices": [{"delta": {}}]}',
                _delta("defines scope."),
                'data: {"choices": [], "usage": {"total_tokens": 12}}',
                "data: [DONE]",
            ),
        )

    adapter = OpenRouterChat(["key"], "model", client=_client(handler))
    parts = list(adapter.stream(TURNS, max_tokens=256))

    assert parts == ["Article ", "5 ", "defines scope."]
    assert seen["body"] == {
        "model": "model",
        "max_tokens": 256,
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is article 5?"},
        ],
    }


def test_stream_logs_usage_from_the_final_chunk(caplog: pytest.LogCaptureFixture) -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(
            lambda _: httpx.Response(
                200,
                content=_sse(
                    _delta("Article 5."),
                    'data: {"choices": [], "usage": {"total_tokens": 12, '
                    '"prompt_tokens_details": {"cached_tokens": 5}}}',
                    "data: [DONE]",
                ),
            )
        ),
    )

    with caplog.at_level(logging.INFO, logger="rag_ingestion.providers.openrouter"):
        list(adapter.stream(TURNS, max_tokens=16))

    assert "cached_tokens" in caplog.text
    assert "12" in caplog.text


def test_stream_stops_at_the_done_sentinel() -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(
            lambda _: httpx.Response(
                200, content=_sse(_delta("kept"), "data: [DONE]", _delta("dropped"))
            )
        ),
    )

    assert list(adapter.stream(TURNS, max_tokens=16)) == ["kept"]


def test_stream_sanitizes_provider_http_errors() -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(lambda _: httpx.Response(429, json={"error": "rate limited"})),
    )

    with pytest.raises(ProviderRequestError, match="Chat provider request failed"):
        list(adapter.stream(TURNS, max_tokens=16))


def test_stream_rejects_a_malformed_chunk() -> None:
    adapter = OpenRouterChat(["key"],
        "model",
        client=_client(lambda _: httpx.Response(200, content=_sse("data: {not json"))),
    )

    with pytest.raises(ProviderResponseError):
        list(adapter.stream(TURNS, max_tokens=16))


def test_transport_failures_are_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    adapter = OpenRouterChat(["key"], "model", client=_client(handler))

    with pytest.raises(ProviderRequestError):
        adapter.complete(TURNS, max_tokens=16)
    with pytest.raises(ProviderRequestError):
        list(adapter.stream(TURNS, max_tokens=16))


def test_api_key_is_never_included_in_error_messages() -> None:
    adapter = OpenRouterChat(["sk-super-secret"],
        "model",
        client=_client(lambda _: httpx.Response(500, text="sk-super-secret leaked")),
    )

    with pytest.raises(ProviderRequestError) as caught:
        adapter.complete(TURNS, max_tokens=16)

    assert "sk-super-secret" not in str(caught.value)


def test_same_sticky_key_always_resolves_the_same_account() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["Authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenRouterChat(["key-a", "key-b"], "model", client=_client(handler))

    for _ in range(4):
        adapter.complete(TURNS, max_tokens=16, sticky_key="thread-1")

    assert len(set(seen_keys)) == 1


def test_different_sticky_keys_spread_across_accounts() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["Authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenRouterChat(["key-a", "key-b"], "model", client=_client(handler))

    for thread_id in (f"thread-{i}" for i in range(20)):
        adapter.complete(TURNS, max_tokens=16, sticky_key=thread_id)

    assert set(seen_keys) == {"Bearer key-a", "Bearer key-b"}


def test_no_sticky_key_round_robins() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["Authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenRouterChat(["key-a", "key-b"], "model", client=_client(handler))

    for _ in range(4):
        adapter.complete(TURNS, max_tokens=16)

    assert seen_keys == ["Bearer key-a", "Bearer key-b", "Bearer key-a", "Bearer key-b"]


def test_stream_also_honours_sticky_key() -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["Authorization"])
        return httpx.Response(200, content=_sse(_delta("hi"), "data: [DONE]"))

    adapter = OpenRouterChat(["key-a", "key-b"], "model", client=_client(handler))

    for _ in range(2):
        list(adapter.stream(TURNS, max_tokens=16, sticky_key="thread-1"))

    assert len(set(seen_keys)) == 1


def test_rejects_an_empty_key_list() -> None:
    with pytest.raises(ValueError):
        OpenRouterChat([], "model")

