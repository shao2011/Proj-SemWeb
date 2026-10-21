"""SQLModel table definitions.

These are storage rows, deliberately separate from the frozen Pydantic
contracts in `storage/projects.py` and `storage/threads.py`. The stores map
rows to those contracts, so routes, response models, and their tests keep
working against immutable objects rather than live ORM instances.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Sequence, UniqueConstraint, text
from sqlmodel import Column, Field, SQLModel

#: Global recency counter for `threads.sequence`.
#:
#: A database sequence rather than `max(sequence) + 1`: the read-then-write was
#: not atomic across transactions, so two threads touched at the same moment
#: could claim one number and the recents order between them came down to the
#: execution plan. Bound to the metadata so `create_all` emits it alongside the
#: tables, and assigned explicitly by the store, since `add_message` has to
#: advance an existing row rather than default a new one.
#:
#: `SQLModel.metadata` directly rather than `db.base.target_metadata`, which is
#: the same object: `db.base` imports this module, so importing it back would
#: close the cycle.
THREAD_RECENCY_SEQUENCE = Sequence("threads_recency_seq", metadata=SQLModel.metadata)


def _timestamptz(name: str) -> Column:
    """Timestamps are stored with their offset.

    The stores work in tz-aware UTC and the API serialises the offset, so a
    naive `timestamp` column would silently drop it on the way through.
    """
    return Column(name, DateTime(timezone=True), nullable=False)


class ProjectRow(SQLModel, table=True):
    __tablename__ = "projects"

    project_id: str = Field(primary_key=True, max_length=64)
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    name: str = Field(nullable=False, max_length=200)
    visibility: str = Field(default="private", nullable=False, max_length=16)
    # The pinned embedding profile is opaque to the database; it is validated by
    # PinnedEmbeddingProfile on the way out.
    embedding_profile: str = Field(nullable=False)
    icon: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))


class ProjectMemberRow(SQLModel, table=True):
    """Someone the project owner added, by address.

    Keyed on the address rather than a uid because that is what an owner types,
    and the person may not have signed up yet. Access resolves it against the
    *verified* address on the caller's token, so an unverified claim matches
    nothing. Stored normalized so the invitation and the token compare equal.

    The owner is deliberately not a row here: ownership lives on
    `ProjectRow.owner_user_id`, and having it in two places would let them
    disagree.
    """

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "email", name="project_members_key"),
    )

    member_id: str = Field(primary_key=True, max_length=64)
    project_id: str = Field(
        foreign_key="projects.project_id",
        ondelete="CASCADE",
        nullable=False,
        max_length=64,
    )
    email: str = Field(index=True, nullable=False, max_length=320)
    #: "viewer" reads and asks; "editor" also uploads, trashes and renames.
    role: str = Field(nullable=False, max_length=16)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))


class ProjectFileRow(SQLModel, table=True):
    __tablename__ = "project_files"

    file_id: str = Field(primary_key=True, max_length=64)
    project_id: str = Field(
        foreign_key="projects.project_id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
        max_length=64,
    )
    filename: str = Field(nullable=False, max_length=512)
    content_hash: str = Field(nullable=False, max_length=128)
    doc_id: str | None = Field(default=None, max_length=128)
    status: str = Field(nullable=False, max_length=16)
    error: str | None = Field(default=None)
    # NULL = active. Set = in the trash: hidden from retrieval, vectors and
    # media kept so a re-upload of the same content within the TTL restores
    # for free instead of re-embedding.
    trashed_at: datetime | None = Field(
        default=None, sa_column=Column("trashed_at", DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))


class ThreadRow(SQLModel, table=True):
    __tablename__ = "threads"
    __table_args__ = (
        Index("threads_owner_sequence_idx", "owner_user_id", "sequence"),
    )

    thread_id: str = Field(primary_key=True, max_length=64)
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    project_id: str | None = Field(default=None, max_length=64)
    title: str = Field(nullable=False, max_length=200)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))
    # Recency ordering key. A timestamp is not usable here: the clock resolution
    # on some hosts puts several writes on one identical value, which made the
    # recents order arbitrary.
    sequence: int = Field(sa_column=Column("sequence", BigInteger, nullable=False))
    # L2: the turns that have scrolled out of the replayed window, in prose.
    summary: str | None = Field(default=None)
    # Watermark. Every message at or below this position is already covered by
    # `summary`, so summarizing is incremental: old summary plus whatever has
    # newly dropped out, never the whole middle again.
    summarized_through_position: int | None = Field(default=None)
    # Measured size of the last prompt, as the provider counted it. This is
    # the context gauge's numerator and the summary trigger's input; chars
    # ride along so chars/tokens calibrates the next turn's estimate, and the
    # model id says which tokenizer produced the figure -- a ratio measured
    # under one model must not calibrate an estimate for another.
    last_prompt_tokens: int | None = Field(default=None)
    last_prompt_chars: int | None = Field(default=None)
    last_model_id: str | None = Field(default=None, max_length=128)


class ThreadMessageRow(SQLModel, table=True):
    __tablename__ = "thread_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "position", name="thread_messages_position_key"),
    )

    message_id: str = Field(primary_key=True, max_length=64)
    thread_id: str = Field(
        foreign_key="threads.thread_id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
        max_length=64,
    )
    role: str = Field(nullable=False, max_length=16)
    content: str = Field(nullable=False)
    citations: str = Field(nullable=False)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    # Conversation order. SQLite's implicit rowid ordering does not exist in
    # Postgres, so message order has to be stored explicitly.
    position: int = Field(nullable=False)


class ThreadShareRow(SQLModel, table=True):
    """A public link to one thread.

    `share_id` is both the primary key and the token in the URL, so it is
    minted unguessable. `shared_through_position` is what makes the link a
    snapshot rather than a live window: positions are only ever appended, so
    everything at or below the watermark is exactly what the owner shared.
    `title` is copied for the same reason -- renaming the chat afterwards must
    not change what a link already handed out shows.

    One live row per thread, enforced by the unique index: revoking deletes
    the row, so a re-share mints a new token and the old link stays dead.
    """

    __tablename__ = "thread_shares"
    __table_args__ = (
        UniqueConstraint("thread_id", name="thread_shares_thread_key"),
    )

    share_id: str = Field(primary_key=True, max_length=64)
    thread_id: str = Field(
        foreign_key="threads.thread_id",
        ondelete="CASCADE",
        nullable=False,
        max_length=64,
    )
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    title: str = Field(nullable=False, max_length=200)
    shared_through_position: int = Field(nullable=False)
    # "link" = anyone holding the URL, the original behaviour and the default
    # for rows written before this column existed. "invite" = only the
    # addresses in `thread_share_recipients`.
    access: str = Field(default="link", nullable=False, max_length=16)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))


class ThreadShareRecipientRow(SQLModel, table=True):
    """One address invited to a share.

    Stored normalized (trimmed, case-folded) so an invitation typed by hand
    and an address read out of a login token compare as equal. Indexed on
    `email` because "what was shared with me" is a lookup by address.
    """

    __tablename__ = "thread_share_recipients"
    __table_args__ = (
        UniqueConstraint("share_id", "email", name="thread_share_recipients_key"),
    )

    recipient_id: str = Field(primary_key=True, max_length=64)
    share_id: str = Field(
        foreign_key="thread_shares.share_id",
        ondelete="CASCADE",
        nullable=False,
        max_length=64,
    )
    email: str = Field(index=True, nullable=False, max_length=320)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))


class MessageReportRow(SQLModel, table=True):
    """A message someone reported, with the chat as it was at that moment.

    `thread_id` is deliberately not a foreign key and the conversation is
    stored inline: a report whose evidence disappears when the reported chat
    is deleted is not evidence. Same reason `MemoryAuditRow` keeps no key back
    to its subject.
    """

    __tablename__ = "message_reports"

    report_id: str = Field(primary_key=True, max_length=64)
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    thread_id: str = Field(index=True, nullable=False, max_length=64)
    #: Which message was reported, 1-based. NULL when the whole chat was.
    message_position: int | None = Field(default=None)
    reason: str = Field(nullable=False, max_length=32)
    details: str | None = Field(default=None)
    #: JSON list of {role, content}, captured server-side at report time.
    conversation: str = Field(nullable=False)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    # NULL = still in the inbox. An inbox with no way to mark something done
    # is a list that only ever grows.
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column("resolved_at", DateTime(timezone=True))
    )
    resolved_by: str | None = Field(default=None, max_length=320)


class MemoryRow(SQLModel, table=True):
    __tablename__ = "memory"
    __table_args__ = (
        # Covers the read path: one query for all three scopes at once.
        Index("memory_owner_scope_idx", "owner_user_id", "scope", "deleted_at"),
        # Counts distinct threads per key, for promoting a local memory to global.
        Index("memory_owner_key_idx", "owner_user_id", "key"),
        # One live row per key per scope, enforced by the database rather than
        # only by the supersede pass in the store. The store serializes writers
        # with an advisory lock, and this is what makes the invariant hold even
        # if a future writer forgets to take it. Partial, because superseded and
        # deleted rows are kept and legitimately repeat the key; the coalesce
        # pairs are needed because NULL never equals NULL in a unique index, so
        # user-scoped rows would otherwise not collide at all.
        Index(
            "memory_live_key_idx",
            "owner_user_id",
            "scope",
            text("coalesce(thread_id, '')"),
            text("coalesce(project_id, '')"),
            "key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    memory_id: str = Field(primary_key=True, max_length=64)
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    scope: str = Field(nullable=False, max_length=16)
    # CASCADE is required: a thread-scoped memory outliving its thread becomes a
    # row that can never be loaded again.
    thread_id: str | None = Field(
        default=None,
        foreign_key="threads.thread_id",
        ondelete="CASCADE",
        max_length=64,
    )
    project_id: str | None = Field(
        default=None,
        foreign_key="projects.project_id",
        ondelete="CASCADE",
        max_length=64,
    )
    type: str = Field(nullable=False, max_length=16)
    key: str = Field(nullable=False, max_length=255)
    value: str = Field(nullable=False)
    source: str = Field(nullable=False, max_length=32)
    superseded_by: str | None = Field(default=None, max_length=64)
    last_used_at: datetime | None = Field(
        default=None, sa_column=Column("last_used_at", DateTime(timezone=True))
    )
    # Marked, never hard deleted, so a bad write stays auditable.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column("deleted_at", DateTime(timezone=True))
    )
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))


class MemoryProposalRow(SQLModel, table=True):
    __tablename__ = "memory_proposal"

    proposal_id: str = Field(primary_key=True, max_length=64)
    owner_user_id: str = Field(index=True, nullable=False, max_length=255)
    thread_id: str = Field(
        foreign_key="threads.thread_id",
        ondelete="CASCADE",
        nullable=False,
        max_length=64,
    )
    suggested_scope: str = Field(nullable=False, max_length=16)
    type: str = Field(nullable=False, max_length=16)
    key: str = Field(nullable=False, max_length=255)
    value: str = Field(nullable=False)
    source_message_id: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))


class MemoryAuditRow(SQLModel, table=True):
    __tablename__ = "memory_audit"

    audit_id: str = Field(primary_key=True, max_length=64)
    # Not a foreign key: the audit trail must survive its subject.
    memory_id: str = Field(index=True, nullable=False, max_length=64)
    op: str = Field(nullable=False, max_length=16)
    source: str = Field(nullable=False, max_length=32)
    source_message_id: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    # Trail order. `created_at` is not enough: the clock resolution on some hosts
    # puts two writes on one identical value, which would make ADD/UPDATE order
    # arbitrary. Same reason ThreadRow carries `sequence`.
    sequence: int = Field(nullable=False)


class UserRow(SQLModel, table=True):
    """Every identity that ever presented a valid login token.

    This registry is what makes account claiming safe on the server: an id
    recorded here belongs to a real account and can never be claimed as
    anonymous history, no matter what the client asserts.
    """

    __tablename__ = "users"

    uid: str = Field(primary_key=True, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    first_login_at: datetime = Field(sa_column=_timestamptz("first_login_at"))
    last_login_at: datetime = Field(sa_column=_timestamptz("last_login_at"))
    # NULL = active. Set = refused by the auth middleware, so a disabled
    # account is locked out of every route rather than of a chosen few.
    disabled_at: datetime | None = Field(
        default=None, sa_column=Column("disabled_at", DateTime(timezone=True))
    )


class AdminUserRow(SQLModel, table=True):
    """An administrator granted from inside the app.

    Keyed on the normalized address rather than a uid, like project members:
    an admin can appoint somebody who has not signed in yet. The bootstrap
    admins from `ADMIN_EMAILS` are deliberately *not* rows here -- they come
    from configuration, so no amount of editing through the API can remove the
    last way in.
    """

    __tablename__ = "admin_users"

    email: str = Field(primary_key=True, max_length=320)
    granted_by: str = Field(nullable=False, max_length=320)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))


class AdminAuditRow(SQLModel, table=True):
    """What an administrator did.

    Administrative power without a trail is power nobody can review, so every
    state-changing admin action writes one row. Not a foreign key to anything:
    the trail has to outlive the account it is about.
    """

    __tablename__ = "admin_audit"

    audit_id: str = Field(primary_key=True, max_length=64)
    actor_email: str = Field(index=True, nullable=False, max_length=320)
    action: str = Field(nullable=False, max_length=32)
    #: The account or report acted on.
    target: str = Field(nullable=False, max_length=320)
    detail: str | None = Field(default=None)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))


class UserSettingsRow(SQLModel, table=True):
    """Per-user switches. Not part of the design document; added for the
    memory-enabled toggle the settings screen needs."""

    __tablename__ = "user_settings"

    owner_user_id: str = Field(primary_key=True, max_length=255)
    memory_enabled: bool = Field(default=True, nullable=False)
    # Filled from the login provider's profile (Google name/email); NULL for
    # anonymous visitors.
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    created_at: datetime = Field(sa_column=_timestamptz("created_at"))
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))


class LlmUsageRow(SQLModel, table=True):
    """Per-user running totals of LLM token usage, incremented after each
    chat completion. An accumulator rather than a per-call log: the settings
    screen only needs lifetime totals, and one row per user cannot grow
    unbounded."""

    __tablename__ = "llm_usage"

    owner_user_id: str = Field(primary_key=True, max_length=255)
    prompt_tokens: int = Field(
        default=0, sa_column=Column("prompt_tokens", BigInteger, nullable=False)
    )
    completion_tokens: int = Field(
        default=0, sa_column=Column("completion_tokens", BigInteger, nullable=False)
    )
    total_tokens: int = Field(
        default=0, sa_column=Column("total_tokens", BigInteger, nullable=False)
    )
    requests: int = Field(default=0, nullable=False)
    updated_at: datetime = Field(sa_column=_timestamptz("updated_at"))
