"""Administration: accounts, grants, the reports inbox, and the audit trail.

Protocol only; the Postgres implementation lives in ``pg_admin``. This store
deliberately reads across tables other stores own (users, usage, reports):
the admin screen is a view over the whole system, and threading five stores
through one router would spread that view across five files.

Bootstrap admins are *not* here. They come from `ADMIN_EMAILS` configuration,
which is what makes the first admin possible and the last one irremovable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AdminUserAccount:
    uid: str
    email: str | None
    display_name: str | None
    first_login_at: datetime
    last_login_at: datetime
    disabled_at: datetime | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    requests: int


@dataclass(frozen=True)
class AdminGrant:
    email: str
    granted_by: str
    created_at: datetime


@dataclass(frozen=True)
class AdminAuditEntry:
    audit_id: str
    actor_email: str
    action: str
    target: str
    detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    owner_user_id: str
    thread_id: str
    message_position: int | None
    reason: str
    details: str | None
    conversation: list[dict[str, str]]
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


@dataclass(frozen=True)
class TopUser:
    email: str | None
    display_name: str | None
    total_tokens: int
    requests: int


@dataclass(frozen=True)
class SystemStats:
    """One read over the whole system for the overview tab.

    Lifetime totals only: `llm_usage` is an accumulator, so there is no
    per-day series to offer until a per-event usage log exists.
    """

    users_total: int
    users_active_7d: int
    users_disabled: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    requests: int
    threads: int
    messages: int
    projects: int
    documents: int
    documents_failed: int
    memories: int
    reports_open: int
    reports_resolved: int
    top_users: list[TopUser]


@runtime_checkable
class AdminStore(Protocol):
    def list_users(self) -> list[AdminUserAccount]: ...

    def stats(self) -> SystemStats: ...

    def get_user(self, uid: str) -> AdminUserAccount | None: ...

    def set_user_disabled(self, *, uid: str, disabled: bool) -> AdminUserAccount | None:
        """Flip the account's lock. Returns the account, or None for an
        unknown uid. Idempotent: flipping to the current state changes
        nothing, which is what lets the caller decide whether to audit."""
        ...

    def is_admin(self, email: str) -> bool: ...

    def list_admins(self) -> list[AdminGrant]: ...

    def grant_admin(self, *, email: str, granted_by: str) -> AdminGrant | None:
        """Returns None when the address is already granted."""
        ...

    def revoke_admin(self, email: str) -> bool: ...

    def list_reports(self, *, include_resolved: bool = False) -> list[ReportRecord]: ...

    def resolve_report(
        self, *, report_id: str, resolved_by: str
    ) -> tuple[ReportRecord, bool] | None:
        """Mark a report handled. Returns (record, changed) — changed is False
        when it was already resolved, so the first resolver stays on record —
        or None for an unknown id."""
        ...

    def record_action(
        self, *, actor_email: str, action: str, target: str, detail: str | None = None
    ) -> None: ...

    def list_audit(self, *, limit: int = 200) -> list[AdminAuditEntry]: ...

    def close(self) -> None: ...
