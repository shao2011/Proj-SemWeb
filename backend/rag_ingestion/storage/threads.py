from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator

from rag_ingestion.models import StrictModel


class ThreadMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Thread(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime
    #: L2: the turns that have scrolled out of the replayed window, in prose.
    summary: str | None = None
    #: Every message at or below this position is already covered by `summary`.
    summarized_through_position: int | None = None
    #: Prompt size of the last turn as the provider measured it, with the
    #: chars that produced it and the model whose tokenizer counted it.
    #: NULL until the thread's first answered turn.
    last_prompt_tokens: int | None = None
    last_prompt_chars: int | None = None
    last_model_id: str | None = None


class Citation(StrictModel):
    """Where a cited passage lives, stored with the message that cited it.

    Enough to open the document panel and highlight the passage without asking
    Qdrant, which matters because reopening an old thread should not depend on
    the vector store being up.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1)
    #: Absent on citations written before this carried a target. Those stay
    #: unclickable rather than pointing somewhere wrong.
    doc_id: str | None = None
    block_ids: list[str] = []


class ThreadMessage(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    role: ThreadMessageRole
    content: str = Field(min_length=1)
    citations: list[Citation] = []
    created_at: datetime
    #: 1-based conversation order. Exposed because `list_messages` returns the
    #: newest slice of a long thread rather than all of it, and a caller
    #: reasoning about the summary watermark, or a client rendering a gap, needs
    #: to know where the slice it holds actually sits.
    position: int = Field(default=1, ge=1)

    @field_validator("citations", mode="before")
    @classmethod
    def accept_bare_chunk_ids(cls, value: object) -> object:
        """Read rows written when a citation was just a chunk id."""
        if isinstance(value, list):
            return [
                {"chunk_id": item} if isinstance(item, str) else item for item in value
            ]
        return value


def normalize_email(value: str) -> str:
    """One spelling per address, so an invitation and a login token agree.

    Case and padding only. The local part is technically case-sensitive per
    RFC 5321, but no provider anyone signs in with treats it that way, and
    matching "Guest@example.com" against an invitation typed in lower case is
    what a person means every time.

    Lives here rather than in the API layer because the store writes and
    queries the normalized form, and two spellings of "normalized" would be a
    silent access-control bug.
    """
    return value.strip().casefold()


class ShareAccess(StrEnum):
    #: Anyone holding the URL.
    LINK = "link"
    #: Only the addresses on the share's recipient list.
    INVITE = "invite"


class ThreadShare(StrictModel):
    """A link to one thread, as a snapshot.

    `shared_through_position` and `title` are copied at share time: message
    positions are only appended, so the pair fully describes what a link
    shows, and later turns or renames cannot change it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Also the token in the URL, so it is minted unguessable.
    share_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    shared_through_position: int = Field(ge=0)
    access: ShareAccess = ShareAccess.LINK
    #: Normalized addresses, empty for a link share.
    recipients: list[str] = []
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class ThreadStore(Protocol):
    def create_thread(
        self,
        *,
        owner_user_id: str,
        title: str,
        project_id: str | None = None,
    ) -> Thread: ...

    def get_thread(self, *, owner_user_id: str, thread_id: str) -> Thread | None: ...

    def list_threads(self, *, owner_user_id: str, limit: int = 50) -> list[Thread]: ...

    def rename_thread(
        self, *, owner_user_id: str, thread_id: str, title: str
    ) -> Thread | None: ...

    def delete_thread(self, *, owner_user_id: str, thread_id: str) -> bool: ...

    def count_user_messages(self, *, owner_user_id: str) -> int: ...

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int: ...

    def set_summary(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        summary: str,
        summarized_through_position: int,
    ) -> Thread | None: ...

    def record_prompt_metrics(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        prompt_tokens: int,
        prompt_chars: int,
        model_id: str,
    ) -> Thread | None:
        """Store the measured size of the turn that just ran.

        Bookkeeping like `set_summary`: it must not bump `sequence` or
        `updated_at`, so recording a measurement never reorders the
        recents list.
        """
        ...

    def add_message(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        role: ThreadMessageRole,
        content: str,
        citations: list[Citation] | None = None,
    ) -> ThreadMessage | None: ...

    def list_messages(
        self, *, owner_user_id: str, thread_id: str, limit: int = 200
    ) -> list[ThreadMessage] | None:
        """The newest `limit` messages, in conversation order.

        Newest, not oldest: this feeds the prompt for the question being asked
        now, so a thread longer than `limit` must give up its beginning rather
        than its present.
        """
        ...

    def list_messages_range(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        from_position: int,
        through_position: int,
        limit: int = 200,
    ) -> list[ThreadMessage] | None:
        """Messages in an inclusive position range, in conversation order.

        What the summarizer folds and where the thread's opening turn is read
        from, both of which sit outside the newest slice on a long thread.
        """
        ...

    def count_messages(self, *, owner_user_id: str, thread_id: str) -> int | None:
        """Total messages, which `len(list_messages(...))` no longer gives."""
        ...

    def fork_thread(
        self, *, owner_user_id: str, thread_id: str, through_position: int, title: str
    ) -> Thread | None:
        """Copy messages 1..`through_position` into a brand new thread.

        A copy, not a move: the original keeps every message it had. The new
        thread starts with no summary and no watermark, because every message
        it holds is verbatim and claiming otherwise would skip turns.
        """
        ...

    def create_report(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        message_position: int | None,
        reason: str,
        details: str | None,
        conversation: list[dict[str, str]],
    ) -> str:
        """Store a report with its own copy of the chat. Returns the id."""
        ...

    # --- sharing ------------------------------------------------------------

    def share_thread(
        self, *, owner_user_id: str, thread_id: str, title: str, through_position: int
    ) -> ThreadShare:
        """Create the thread's link, or return the one it already has.

        Never re-points an existing link on its own: extending the snapshot is
        an explicit act, so the caller refreshes through `refresh_share`.
        """
        ...

    def refresh_share(
        self, *, owner_user_id: str, thread_id: str, title: str, through_position: int
    ) -> ThreadShare | None:
        """Move an existing link's snapshot forward, keeping its token."""
        ...

    def get_thread_share(
        self, *, owner_user_id: str, thread_id: str
    ) -> ThreadShare | None: ...

    def get_share(self, *, share_id: str) -> ThreadShare | None:
        """Resolve a token. Deliberately owner-free: this backs the public
        route, where the token itself is the only credential."""
        ...

    def revoke_share(self, *, owner_user_id: str, thread_id: str) -> bool: ...

    def set_share_access(
        self, *, owner_user_id: str, thread_id: str, access: ShareAccess
    ) -> ThreadShare | None: ...

    def add_share_recipient(
        self, *, owner_user_id: str, thread_id: str, email: str
    ) -> ThreadShare | None:
        """Invite one address, and close the link if it was still open.

        Idempotent: inviting the same person twice is a no-op, not an error.
        """
        ...

    def remove_share_recipient(
        self, *, owner_user_id: str, thread_id: str, email: str
    ) -> ThreadShare | None: ...

    def list_shares_for_email(self, *, email: str) -> list[ThreadShare]:
        """Every invite share this address may open. Backs "Shared with you"."""
        ...

    def fork_share(
        self, *, share: ThreadShare, new_owner_user_id: str
    ) -> Thread | None:
        """Copy a share's snapshot into a thread owned by the reader.

        `project_id` is left NULL and citations are dropped: the copy must not
        be able to retrieve over the owner's documents, and the owner's chunk
        and document ids are not the reader's to hold.
        """
        ...

    def close(self) -> None: ...
