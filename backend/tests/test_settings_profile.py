"""The Google profile (name, email) lands in user settings at login."""

from __future__ import annotations

from rag_ingestion.memory.store import PostgresMemoryStore


def test_update_profile_persists_name_and_email(pg_engine) -> None:
    store = PostgresMemoryStore(pg_engine)

    updated = store.update_profile(
        owner_user_id="u1", display_name="Đạt Hoàng", email="dat@example.com"
    )

    assert updated.display_name == "Đạt Hoàng"
    assert updated.email == "dat@example.com"

    again = store.get_settings(owner_user_id="u1")
    assert again.display_name == "Đạt Hoàng"
    assert again.email == "dat@example.com"
    # Creating the row through a profile write keeps the memory default.
    assert again.memory_enabled is True


def test_patch_settings_accepts_profile_fields_and_partial_updates(pg_engine) -> None:
    from tests.test_thread_api import _setup

    client, _, _qdrant, _bundle = _setup(
        pg_engine, memory=PostgresMemoryStore(pg_engine)
    )
    headers = {"X-User-ID": "u2"}

    saved = client.patch(
        "/settings",
        json={"display_name": "Đạt", "email": "d@example.com"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["display_name"] == "Đạt"
    assert saved.json()["email"] == "d@example.com"

    # The memory toggle alone must not wipe the profile.
    toggled = client.patch(
        "/settings", json={"memory_enabled": False}, headers=headers
    )
    assert toggled.json()["memory_enabled"] is False
    assert toggled.json()["display_name"] == "Đạt"


def test_claiming_an_account_carries_the_profile(pg_engine) -> None:
    store = PostgresMemoryStore(pg_engine)
    store.update_profile(
        owner_user_id="anon-1", display_name="Anon Name", email="anon@example.com"
    )

    store.reassign_owner(anonymous_user_id="anon-1", verified_user_id="uid-1")

    claimed = store.get_settings(owner_user_id="uid-1")
    assert claimed.display_name == "Anon Name"
    assert claimed.email == "anon@example.com"
