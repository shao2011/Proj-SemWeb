"""Memory tools as standalone domain objects.

The route-level behaviour is covered end to end in `test_thread_api.py`;
these pin the parts that must hold no matter who calls them.
"""

from __future__ import annotations

from rag_ingestion.memory.models import MemoryScope, MemoryType
from rag_ingestion.memory import build_memory_tools


class FakeWriter:
    def __init__(self, *, enabled: bool = True, saved: bool = True) -> None:
        self.enabled = enabled
        self.saved = saved
        self.existing: set[tuple[str, str]] = set()
        self.writes: list[tuple[MemoryScope, str, str, MemoryType]] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def already_saved(self, *, key: str, value: str) -> bool:
        return (key, value) in self.existing

    def save(self, *, scope, key, value, type) -> bool:
        self.writes.append((scope, key, value, type))
        return self.saved


def _call(tools, name: str, **arguments) -> str:
    tool = next(item for item in tools if item.name == name)
    return tool.invoke(arguments)


def test_a_project_thread_offers_project_scoped_local_memory() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id="prj_1")

    assert [tool.name for tool in tools] == [
        "save_project_memory",
        "save_user_memory",
    ]
    assert (
        _call(
            tools,
            "save_project_memory",
            key="other",
            custom_key="deadline",
            value="March 15",
        )
        == "saved"
    )
    assert writer.writes == [
        (MemoryScope.PROJECT, "other:deadline", "March 15", MemoryType.FACT)
    ]


def test_a_plain_chat_offers_conversation_scoped_local_memory() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    assert [tool.name for tool in tools] == [
        "save_conversation_memory",
        "save_user_memory",
    ]
    _call(tools, "save_conversation_memory", key="response_length", value="short", type="preference")
    assert writer.writes == [
        (MemoryScope.THREAD, "response_length", "short", MemoryType.PREFERENCE)
    ]


def test_the_global_tool_always_writes_user_scope() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id="prj_1")

    _call(tools, "save_user_memory", key="nickname", value="Dat")

    assert writer.writes == [(MemoryScope.USER, "nickname", "Dat", MemoryType.FACT)]


def test_an_unchanged_value_is_not_written_again() -> None:
    writer = FakeWriter()
    writer.existing.add(("nickname", "Dat"))
    tools = build_memory_tools(writer, project_id=None)

    assert _call(tools, "save_user_memory", key="nickname", value="Dat") == "already saved"
    assert writer.writes == []


def test_memory_switched_off_short_circuits_before_the_store() -> None:
    writer = FakeWriter(enabled=False)
    tools = build_memory_tools(writer, project_id=None)

    result = _call(tools, "save_user_memory", key="nickname", value="Dat")

    assert result == "skipped: the user has memory turned off"
    assert writer.writes == []


def test_a_full_scope_is_reported_as_a_proposal_not_a_silent_drop() -> None:
    writer = FakeWriter(saved=False)
    tools = build_memory_tools(writer, project_id=None)

    result = _call(tools, "save_user_memory", key="nickname", value="Dat")

    assert result == "saved as a proposal (scope was full)"


def test_blank_or_unknown_arguments_never_reach_the_store() -> None:
    """The schema rejects most of this before the tool body runs; a blank
    value is the one case that gets through as a string."""
    import pydantic
    import pytest

    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    with pytest.raises(pydantic.ValidationError):
        _call(tools, "save_user_memory", key="not_a_slot", value="Dat")
    with pytest.raises(pydantic.ValidationError):
        _call(tools, "save_user_memory", key="nickname", value="")
    assert _call(tools, "save_user_memory", key="nickname", value="   ").startswith(
        "error"
    )
    assert writer.writes == []


def test_oversized_arguments_are_truncated_to_the_column_limits() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    _call(
        tools,
        "save_user_memory",
        key="other",
        custom_key="k" * 400,
        value="v" * 5_000,
    )

    _scope, key, value, _type = writer.writes[0]
    # The custom name is normalized into the store's `other:<slug>` shape,
    # whose slug is capped at 63 characters; the value keeps its own limit.
    assert key == "other:" + "k" * 63
    assert len(value) == 2_000


# --- key normalization -------------------------------------------------------
#
# Storage enforces a closed key list; a live run had the model save `name` and
# `profession`, which would have been rejected after the answer was already
# streamed and the user could never be told why.


def test_synonyms_of_closed_keys_are_mapped_not_rejected() -> None:
    from rag_ingestion.memory.tools import normalize_key

    assert normalize_key("name") == "nickname"
    assert normalize_key("profession") == "occupation"
    assert normalize_key("Company") == "organization"
    assert normalize_key("preferred language") == "response_language"


def test_closed_keys_pass_through_untouched() -> None:
    from rag_ingestion.memory.tools import normalize_key

    assert normalize_key("nickname") == "nickname"
    assert normalize_key("other:deadline") == "other:deadline"


def test_anything_else_becomes_an_other_slug_the_store_accepts() -> None:
    from rag_ingestion.memory.models import is_valid_key
    from rag_ingestion.memory.tools import normalize_key

    for raw in ["favourite_food", "Pet Name", "dự án", "weird!!key"]:
        normalized = normalize_key(raw)
        assert is_valid_key(normalized), (raw, normalized)


def test_every_saved_key_is_storable() -> None:
    """Whatever route a key takes -- a named slot or a custom name -- storage
    has to accept it, because a rejection surfaces after the answer has
    already streamed."""
    from rag_ingestion.memory.models import is_valid_key

    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    _call(tools, "save_user_memory", key="occupation", value="accountant")
    _call(
        tools,
        "save_user_memory",
        key="other",
        custom_key="Favourite editor",
        value="vim",
    )
    # A model that puts a known slot name in custom_key still lands on it.
    _call(
        tools, "save_user_memory", key="other", custom_key="profession", value="auditor"
    )

    assert [key for _scope, key, _value, _type in writer.writes] == [
        "occupation",
        "other:favourite-editor",
        "occupation",
    ]
    assert all(is_valid_key(key) for _s, key, _v, _t in writer.writes)



# --- typed tool arguments ------------------------------------------------------
#
# The key used to be a bare string with a hint in its description, and a live
# model answered with `name` and `profession`: valid English, invalid keys.
# The schema now names the choices, so the provider constrains them.


def test_the_tool_schema_offers_the_closed_key_list_as_an_enum() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)
    schema = tools[1].args_schema.model_json_schema()

    slot = schema["$defs"]["MemorySlot"]["enum"]
    assert "nickname" in slot
    assert "occupation" in slot
    assert "other" in slot, "the escape hatch has to be reachable through the enum"
    assert "name" not in slot


def test_the_type_argument_is_an_enum_too() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)
    schema = tools[1].args_schema.model_json_schema()

    assert set(schema["$defs"]["MemoryType"]["enum"]) == {"fact", "preference"}


def test_the_other_slot_takes_its_name_from_custom_key() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    _call(
        tools,
        "save_user_memory",
        key="other",
        custom_key="Favourite editor",
        value="vim",
    )

    assert writer.writes == [
        (MemoryScope.USER, "other:favourite-editor", "vim", MemoryType.FACT)
    ]


def test_the_other_slot_without_a_name_is_refused() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    result = _call(tools, "save_user_memory", key="other", value="vim")

    assert result.startswith("error")
    assert writer.writes == []


def test_a_named_slot_ignores_a_stray_custom_key() -> None:
    writer = FakeWriter()
    tools = build_memory_tools(writer, project_id=None)

    _call(
        tools, "save_user_memory", key="nickname", custom_key="ignored", value="Dat"
    )

    assert writer.writes == [(MemoryScope.USER, "nickname", "Dat", MemoryType.FACT)]
