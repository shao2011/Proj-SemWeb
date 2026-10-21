"""Memory contracts.

Implements the storage half of the multi-turn memory design: three scopes, the
closed key list, and the four operations. Reading memory into the prompt is a
separate step and is deliberately not wired here yet.

The design document writes the closed key list in Vietnamese; the identifiers
below are the English equivalents, since the codebase is English-only.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator, model_validator

from rag_ingestion.models import StrictModel


class MemoryScope(StrEnum):
    """`thread` dies with its thread, `user` is injected everywhere."""

    THREAD = "thread"
    PROJECT = "project"
    USER = "user"


class MemoryType(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"


class MemorySource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    USER_CONFIRMED = "user_confirmed"
    LLM_EXTRACTED = "llm_extracted"


class MemoryKey(StrEnum):
    """Closed key list.

    Supersede matches on `key`, so a free-form key drifts ("occupation" one
    turn, "job" the next) and two contradictory memories end up coexisting.
    Anything outside this list must use the `other:<slug>` branch, where
    supersede is weaker by design.
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


_OTHER_KEY = re.compile(r"^other:[a-z0-9][a-z0-9_-]{0,63}$")

#: Per-scope ceilings, sized by how long the scope lives.
#:
#: A thread and a project are bounded contexts that end, so their memory can
#: stay small and the oldest can go when the scope fills. User scope outlives
#: everything and is the only one worth keeping more of -- fifty, against a
#: prompt that carries twenty-five, which leaves room to hold more than any
#: one answer needs without letting the table grow without end.
#:
#: A full scope evicts rather than refusing (see `_evict_least_recently_used`),
#: so these decide what is forgotten, not what can be written.
MEMORY_CAPS: dict[MemoryScope, int] = {
    MemoryScope.THREAD: 10,
    MemoryScope.PROJECT: 30,
    MemoryScope.USER: 50,
}


def is_valid_key(key: str) -> bool:
    if key in {item.value for item in MemoryKey}:
        return True
    return _OTHER_KEY.match(key) is not None


class MemoryOperation(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class Memory(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    scope: MemoryScope
    thread_id: str | None = None
    project_id: str | None = None
    type: MemoryType
    key: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=2_000)
    source: MemorySource
    superseded_by: str | None = None
    #: Reserved, and deliberately never written yet. It was meant to rank
    #: memories by when they last reached a prompt, but selection is
    #: deterministic from the same ranking, so writing it only locked in
    #: whatever the first `created_at` ordering had chosen -- no information,
    #: one UPDATE per turn. `last_relevant_at` already reads it, so a survey
    #: that finds a signal worth recording can turn it back on without
    #: touching the ranking.
    last_used_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not is_valid_key(value):
            raise ValueError(
                "Key must be one of the closed list or match other:<slug>"
            )
        return value

    @model_validator(mode="after")
    def validate_scope_target(self) -> Memory:
        if self.scope is MemoryScope.THREAD and not self.thread_id:
            raise ValueError("Thread-scoped memory requires a thread_id")
        if self.scope is MemoryScope.PROJECT and not self.project_id:
            raise ValueError("Project-scoped memory requires a project_id")
        if self.scope is MemoryScope.USER and (self.thread_id or self.project_id):
            raise ValueError("User-scoped memory must not target a thread or project")
        return self


class MemoryAuditEntry(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    op: MemoryOperation
    source: MemorySource
    source_message_id: str | None = None
    created_at: datetime


class MemoryProposal(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    suggested_scope: MemoryScope
    type: MemoryType
    key: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=2_000)
    source_message_id: str | None = None
    created_at: datetime


class UserSettings(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_user_id: str = Field(min_length=1)
    memory_enabled: bool = True
    #: From the login provider's profile; None for anonymous visitors.
    display_name: str | None = None
    email: str | None = None
    created_at: datetime
    updated_at: datetime


class LlmUsage(StrictModel):
    """Lifetime token totals for one user, as accumulated from provider
    usage reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_user_id: str = Field(min_length=1)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0


@runtime_checkable
class MemoryStore(Protocol):
    def add_memory(
        self,
        *,
        owner_user_id: str,
        scope: MemoryScope,
        type: MemoryType,
        key: str,
        value: str,
        source: MemorySource,
        thread_id: str | None = None,
        project_id: str | None = None,
        #: The message that caused the write, carried into the audit entry.
        source_message_id: str | None = None,
    ) -> Memory: ...

    def list_memories(
        self,
        *,
        owner_user_id: str,
        scope: MemoryScope | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]: ...

    def recall(
        self,
        *,
        owner_user_id: str,
        thread_id: str | None = None,
        project_id: str | None = None,
    ) -> list[Memory]:
        """Everything applying to one turn: a union over the three scopes.

        Distinct from `list_memories`, which filters. Filtering on `thread_id`
        excludes user-scoped rows, whose `thread_id` is NULL.
        """
        ...

    def distinct_thread_count(self, *, owner_user_id: str, key: str) -> int:
        """How many separate threads hold a live thread-scoped memory on `key`.

        The evidence behind promoting a situational preference to user scope: one
        thread is a request about that conversation, two is a habit.
        """
        ...

    def get_memory(self, *, owner_user_id: str, memory_id: str) -> Memory | None: ...

    def update_memory(
        self,
        *,
        owner_user_id: str,
        memory_id: str,
        value: str | None = None,
        promote_to_user: bool = False,
    ) -> Memory | None: ...

    def delete_memory(self, *, owner_user_id: str, memory_id: str) -> bool: ...

    def add_proposal(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        suggested_scope: MemoryScope,
        type: MemoryType,
        key: str,
        value: str,
        source_message_id: str | None = None,
    ) -> MemoryProposal: ...

    def list_proposals(
        self, *, owner_user_id: str, limit: int = 50
    ) -> list[MemoryProposal]: ...

    def confirm_proposal(
        self, *, owner_user_id: str, proposal_id: str
    ) -> Memory | None: ...

    def dismiss_proposal(self, *, owner_user_id: str, proposal_id: str) -> bool: ...

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int: ...

    def get_settings(self, *, owner_user_id: str) -> UserSettings: ...

    def update_profile(
        self,
        *,
        owner_user_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> UserSettings: ...

    def set_memory_enabled(
        self, *, owner_user_id: str, enabled: bool
    ) -> UserSettings: ...

    def add_llm_usage(
        self, *, owner_user_id: str, prompt_tokens: int, completion_tokens: int
    ) -> None: ...

    def get_llm_usage(self, *, owner_user_id: str) -> LlmUsage: ...
