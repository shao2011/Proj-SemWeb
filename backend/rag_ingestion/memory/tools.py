"""Memory tools the answering model calls inline.

The ChatGPT-`bio` pattern: the model generating the reply decides what is
worth remembering and emits a tool call, so extraction costs no extra LLM
round and needs no language-specific heuristics. Scope belongs to the tool,
never to the model -- local rides the project (or the conversation, in plain
chat), global rides the user.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from rag_ingestion.logger import get_logger
from rag_ingestion.memory.models import MemoryKey, MemoryScope, MemoryType, is_valid_key

_log = get_logger(__name__)

_KEY_MAX = 255
_VALUE_MAX = 2_000
_SLUG_STRIP = re.compile(r"[^a-z0-9_-]+")

#: Keys models reach for that mean an existing slot. Mapped rather than
#: rejected: the model is right about the fact and only wrong about the label,
#: and a rejection would cost another round trip to fix a synonym.
_KEY_ALIASES = {
    "name": MemoryKey.NICKNAME.value,
    "user_name": MemoryKey.NICKNAME.value,
    "full_name": MemoryKey.NICKNAME.value,
    "profession": MemoryKey.OCCUPATION.value,
    "job": MemoryKey.OCCUPATION.value,
    "role": MemoryKey.OCCUPATION.value,
    "company": MemoryKey.ORGANIZATION.value,
    "employer": MemoryKey.ORGANIZATION.value,
    "language": MemoryKey.RESPONSE_LANGUAGE.value,
    "preferred_language": MemoryKey.RESPONSE_LANGUAGE.value,
    "length": MemoryKey.RESPONSE_LENGTH.value,
    "answer_length": MemoryKey.RESPONSE_LENGTH.value,
    "format": MemoryKey.RESPONSE_FORMAT.value,
    "style": MemoryKey.TONE.value,
    "interest": MemoryKey.FOCUS_AREA.value,
    "interests": MemoryKey.FOCUS_AREA.value,
    "level": MemoryKey.EXPERTISE_LEVEL.value,
}


def normalize_key(key: str) -> str:
    """Map whatever the model produced onto a key the store will accept.

    Storage enforces a closed list plus `other:<slug>`. A model that says
    `profession` instead of `occupation` is right about the memory and wrong
    about the label, so aliases resolve and everything else becomes an
    `other:` slug rather than an error the user would never see explained.
    """
    candidate = key.strip().casefold().replace(" ", "_").replace("-", "_")
    candidate = _KEY_ALIASES.get(candidate, candidate)
    if is_valid_key(candidate):
        return candidate
    slug = _SLUG_STRIP.sub("-", candidate.replace("_", "-")).strip("-")[:63]
    return f"other:{slug}" if slug else ""


class MemorySlot(StrEnum):
    """The key, as the model is allowed to express it.

    A mirror of `MemoryKey` plus `other`, rather than `MemoryKey` itself:
    storage has no `other` member (it stores `other:<slug>`), and the model
    needs one enum covering both cases. Declaring it as an enum is what makes
    the provider constrain the argument -- as a plain string with a hint in
    its description, a live model answered `name` and `profession`.
    """

    RESPONSE_LANGUAGE = "response_language"
    RESPONSE_LENGTH = "response_length"
    RESPONSE_FORMAT = "response_format"
    TONE = "tone"
    NICKNAME = "nickname"
    OCCUPATION = "occupation"
    ORGANIZATION = "organization"
    FOCUS_AREA = "focus_area"
    EXPERTISE_LEVEL = "expertise_level"
    OTHER = "other"


class SaveMemoryArgs(BaseModel):
    """The tool's arguments, typed so the schema names every choice."""

    key: MemorySlot = Field(
        description="Which slot this belongs in. Use 'other' only when nothing else fits."
    )
    value: str = Field(
        min_length=1, description="What the user stated, verbatim in substance."
    )
    type: MemoryType = Field(
        default=MemoryType.FACT,
        description="'fact' for something true about the user, 'preference' for how they want answers.",
    )
    custom_key: str | None = Field(
        default=None,
        description="Required when key is 'other': a two or three word name for the slot.",
    )


class MemoryWriter(Protocol):
    """What a memory tool needs from storage, and nothing more.

    Keeps this module free of the API layer: the caller supplies something
    that already knows the tenant and the cap/proposal policy.
    """

    def is_enabled(self) -> bool: ...

    def already_saved(self, *, key: str, value: str) -> bool: ...

    def save(
        self, *, scope: MemoryScope, key: str, value: str, type: MemoryType
    ) -> bool: ...


def _write(
    writer: MemoryWriter,
    scope: MemoryScope,
    arguments: SaveMemoryArgs,
) -> str:
    """One save, returning the string the model reads as the tool result."""
    if arguments.key is MemorySlot.OTHER:
        if not (arguments.custom_key or "").strip():
            return "error: key 'other' needs a custom_key naming the slot"
        clean_key = normalize_key(arguments.custom_key or "")[:_KEY_MAX]
    else:
        # A named slot is already a valid key; `custom_key` alongside one is
        # the model hedging, and the slot it picked is the answer.
        clean_key = arguments.key.value
    clean_value = arguments.value.strip()[:_VALUE_MAX]
    if not clean_key or not clean_value:
        return "error: key and value are both required"
    if not writer.is_enabled():
        return "skipped: the user has memory turned off"
    if writer.already_saved(key=clean_key, value=clean_value):
        return "already saved"
    memory_type = arguments.type
    written = writer.save(
        scope=scope, key=clean_key, value=clean_value, type=memory_type
    )
    _log.info(
        "Memory tool wrote scope=%s key=%s (written=%s)",
        scope.value,
        clean_key,
        written,
        )
    return "saved" if written else "saved as a proposal (scope was full)"


def build_memory_tools(
    writer: MemoryWriter, *, project_id: str | None
) -> list[BaseTool]:
    """The two tools for one turn, named for where their memory lands.

    A project thread offers `save_project_memory`; a plain chat offers
    `save_conversation_memory`. The name is the user-visible contract for
    scope, which is why it changes with the thread rather than being a
    parameter the model could get wrong.
    """
    local_scope = MemoryScope.PROJECT if project_id else MemoryScope.THREAD
    local_name = "save_project_memory" if project_id else "save_conversation_memory"
    local_context = "this project" if project_id else "this conversation"

    @tool(local_name, args_schema=SaveMemoryArgs)
    def save_local_memory(**arguments) -> str:
        return _write(writer, local_scope, SaveMemoryArgs(**arguments))

    save_local_memory.description = (
        f"Remember something the user stated that only matters inside {local_context} "
        "(current work, a situational preference). Use sparingly: most messages "
        "need no memory at all."
    )

    @tool("save_user_memory", args_schema=SaveMemoryArgs)
    def save_user_memory(**arguments) -> str:
        return _write(writer, MemoryScope.USER, SaveMemoryArgs(**arguments))

    save_user_memory.description = (
        "Remember a durable fact or preference about the user that applies "
        "everywhere: their name, language, profession, or a standing instruction. "
        "Use sparingly: most messages need no memory at all."
    )

    return [save_local_memory, save_user_memory]
