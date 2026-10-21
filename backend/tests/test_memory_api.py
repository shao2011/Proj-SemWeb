from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import Engine

from rag_ingestion.api.routes import create_query_app
from rag_ingestion.providers.registry import ProviderRegistry
from rag_ingestion.memory.models import MEMORY_CAPS, MemoryScope
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore


def _client(pg_engine: Engine, *, with_memory: bool = True) -> TestClient:
    app = create_query_app(
        QdrantClient(location=":memory:"),
        ProviderRegistry([]),
        PostgresProjectStore(pg_engine),
        threads=PostgresThreadStore(pg_engine),
        memory=PostgresMemoryStore(pg_engine) if with_memory else None,
    )
    return TestClient(app)


def _headers(user: str = "user-a") -> dict[str, str]:
    return {"X-User-ID": user}


def _payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "scope": "user",
        "type": "preference",
        "key": "response_length",
        "value": "Keep answers short.",
    }
    payload.update(overrides)
    return payload


# --- routing ----------------------------------------------------------------


def test_proposals_path_is_not_swallowed_by_the_memory_id_route(
    pg_engine: Engine,
) -> None:
    """`/memory/proposals` must resolve to the literal route, not `{memory_id}`."""
    client = _client(pg_engine)

    response = client.get("/memory/proposals", headers=_headers())

    assert response.status_code == 200
    assert response.json() == []


def test_memory_routes_report_unavailable_without_a_store(pg_engine: Engine) -> None:
    client = _client(pg_engine, with_memory=False)

    assert client.get("/memory", headers=_headers()).status_code == 503
    assert client.get("/settings", headers=_headers()).status_code == 503


# --- create -----------------------------------------------------------------


def test_creating_a_user_memory(pg_engine: Engine) -> None:
    client = _client(pg_engine)

    response = client.post("/memory", json=_payload(), headers=_headers())

    assert response.status_code == 201
    body = response.json()
    assert body["scope"] == "user"
    assert body["key"] == "response_length"
    assert body["source"] == "user_explicit"
    assert body["memory_id"].startswith("mem_")
    assert body["deleted_at"] is None


def test_an_unknown_memory_type_is_refused(pg_engine: Engine) -> None:
    """`pin` used to be a third type that nothing read; it is gone entirely.

    Pinning a document belongs on the document, not in memory: a flag on the
    file plus a retrieval filter, whenever that is built.
    """
    client = _client(pg_engine)

    response = client.post(
        "/memory",
        json=_payload(type="pin", key="other:pinned-doc", value="Focus on the 2026 policy."),
        headers=_headers(),
    )

    assert response.status_code == 422
    assert client.get("/memory", headers=_headers()).json() == []


def test_a_key_outside_the_closed_list_is_rejected(pg_engine: Engine) -> None:
    client = _client(pg_engine)

    assert client.post("/memory", json=_payload(key="job"), headers=_headers()).status_code == 422
    assert (
        client.post("/memory", json=_payload(key="other:BAD"), headers=_headers()).status_code
        == 422
    )


def test_blank_value_is_rejected(pg_engine: Engine) -> None:
    client = _client(pg_engine)

    assert client.post("/memory", json=_payload(value="   "), headers=_headers()).status_code == 422


def test_unknown_field_is_rejected(pg_engine: Engine) -> None:
    client = _client(pg_engine)

    response = client.post(
        "/memory", json=_payload(source="llm_extracted"), headers=_headers()
    )

    assert response.status_code == 422


def test_thread_scope_requires_an_owned_thread(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]

    assert (
        client.post(
            "/memory",
            json=_payload(scope="thread", thread_id=thread_id),
            headers=_headers(),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/memory", json=_payload(scope="thread"), headers=_headers()
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/memory",
            json=_payload(scope="thread", thread_id=thread_id),
            headers=_headers("user-b"),
        ).status_code
        == 404
    )


def test_cap_returns_conflict_rather_than_a_server_error(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    for index in range(MEMORY_CAPS[MemoryScope.USER]):
        assert (
            client.post(
                "/memory",
                json=_payload(key=f"other:fact-{index}", value=f"Fact {index}"),
                headers=_headers(),
            ).status_code
            == 201
        )

    response = client.post(
        "/memory", json=_payload(key="other:overflow", value="Too many"), headers=_headers()
    )

    assert response.status_code == 409
    assert "cap" in response.json()["detail"]


# --- list -------------------------------------------------------------------


def test_listing_is_owner_scoped_and_filterable(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    client.post("/memory", json=_payload(value="Global."), headers=_headers())
    client.post(
        "/memory",
        json=_payload(scope="thread", thread_id=thread_id, value="Local."),
        headers=_headers(),
    )

    assert len(client.get("/memory", headers=_headers()).json()) == 2
    assert client.get("/memory", headers=_headers("user-b")).json() == []

    user_only = client.get("/memory?scope=user", headers=_headers()).json()
    assert [item["value"] for item in user_only] == ["Global."]


# --- update -----------------------------------------------------------------


def test_editing_a_memory_value(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    memory_id = client.post("/memory", json=_payload(), headers=_headers()).json()["memory_id"]

    response = client.patch(
        f"/memory/{memory_id}", json={"value": "Be brief."}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.json()["value"] == "Be brief."


def test_promoting_a_thread_memory_to_user_scope(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    memory_id = client.post(
        "/memory",
        json=_payload(scope="thread", thread_id=thread_id),
        headers=_headers(),
    ).json()["memory_id"]

    response = client.patch(
        f"/memory/{memory_id}", json={"promote_to_user": True}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.json()["scope"] == "user"
    assert response.json()["thread_id"] is None


def test_an_empty_patch_is_rejected(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    memory_id = client.post("/memory", json=_payload(), headers=_headers()).json()["memory_id"]

    assert client.patch(f"/memory/{memory_id}", json={}, headers=_headers()).status_code == 422


def test_updating_another_users_memory_is_not_found(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    memory_id = client.post("/memory", json=_payload(), headers=_headers()).json()["memory_id"]

    assert (
        client.patch(
            f"/memory/{memory_id}", json={"value": "stolen"}, headers=_headers("user-b")
        ).status_code
        == 404
    )


# --- delete -----------------------------------------------------------------


def test_deleting_a_memory(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    memory_id = client.post("/memory", json=_payload(), headers=_headers()).json()["memory_id"]

    assert (
        client.delete(f"/memory/{memory_id}", headers=_headers("user-b")).status_code == 404
    )
    assert client.delete(f"/memory/{memory_id}", headers=_headers()).status_code == 204
    assert client.get("/memory", headers=_headers()).json() == []
    assert client.delete(f"/memory/{memory_id}", headers=_headers()).status_code == 404


# --- proposals --------------------------------------------------------------


def test_confirming_a_proposal_through_the_api(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    proposal = PostgresMemoryStore(pg_engine).add_proposal(
        owner_user_id="user-a",
        thread_id=thread_id,
        suggested_scope=MemoryScope.USER,
        type="preference",
        key="tone",
        value="Friendly.",
    )

    listed = client.get("/memory/proposals", headers=_headers()).json()
    assert [item["proposal_id"] for item in listed] == [proposal.proposal_id]

    confirmed = client.post(
        f"/memory/proposals/{proposal.proposal_id}/confirm", headers=_headers()
    )

    assert confirmed.status_code == 201
    assert confirmed.json()["source"] == "user_confirmed"
    assert client.get("/memory/proposals", headers=_headers()).json() == []


def test_dismissing_a_proposal_through_the_api(pg_engine: Engine) -> None:
    client = _client(pg_engine)
    thread_id = client.post("/threads", json={}, headers=_headers()).json()["thread_id"]
    proposal = PostgresMemoryStore(pg_engine).add_proposal(
        owner_user_id="user-a",
        thread_id=thread_id,
        suggested_scope=MemoryScope.USER,
        type="fact",
        key="other:client",
        value="ACME.",
    )

    assert (
        client.delete(
            f"/memory/proposals/{proposal.proposal_id}", headers=_headers("user-b")
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/memory/proposals/{proposal.proposal_id}", headers=_headers()
        ).status_code
        == 204
    )
    assert client.get("/memory", headers=_headers()).json() == []


# --- settings ---------------------------------------------------------------


def test_settings_round_trip(pg_engine: Engine) -> None:
    client = _client(pg_engine)

    assert client.get("/settings", headers=_headers()).json()["memory_enabled"] is True

    patched = client.patch(
        "/settings", json={"memory_enabled": False}, headers=_headers()
    )

    assert patched.status_code == 200
    assert patched.json()["memory_enabled"] is False
    assert client.get("/settings", headers=_headers()).json()["memory_enabled"] is False
    assert client.get("/settings", headers=_headers("user-b")).json()["memory_enabled"] is True
