"""The admin surface: user accounts, the reports inbox, grants, and the trail.

Every route starts with `ctx.require_admin()`, which resolves standing from
the caller's *verified* address — bootstrap admins from `ADMIN_EMAILS`,
granted admins from `admin_users`. The bootstrap set is the floor: the API
refuses to revoke or disable those accounts, so no sequence of admin actions
can lock everybody out of this screen.

Every state change writes one `admin_audit` row. Refused and no-op calls
write none, so the trail reads as what actually happened.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException, status
from pydantic import Field

from rag_ingestion.api.deps import ApiContext
from rag_ingestion.models import StrictModel
from rag_ingestion.storage.admin import (
    AdminAuditEntry,
    AdminGrant,
    AdminUserAccount,
    ReportRecord,
    SystemStats,
)
from rag_ingestion.storage.threads import normalize_email


class AdminMeView(StrictModel):
    email: str
    bootstrap: bool


class UsageView(StrictModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    requests: int


class AdminUserView(StrictModel):
    uid: str
    email: str | None
    display_name: str | None
    first_login_at: datetime
    last_login_at: datetime
    disabled_at: datetime | None
    usage: UsageView

    @classmethod
    def from_account(cls, account: AdminUserAccount) -> AdminUserView:
        return cls(
            uid=account.uid,
            email=account.email,
            display_name=account.display_name,
            first_login_at=account.first_login_at,
            last_login_at=account.last_login_at,
            disabled_at=account.disabled_at,
            usage=UsageView(
                prompt_tokens=account.prompt_tokens,
                completion_tokens=account.completion_tokens,
                total_tokens=account.total_tokens,
                requests=account.requests,
            ),
        )


class UserCountsView(StrictModel):
    total: int
    active_7d: int
    disabled: int


class ContentCountsView(StrictModel):
    threads: int
    messages: int
    projects: int
    documents: int
    documents_failed: int
    memories: int


class ReportCountsView(StrictModel):
    open: int
    resolved: int


class TopUserView(StrictModel):
    email: str | None
    display_name: str | None
    total_tokens: int
    requests: int


class StatsView(StrictModel):
    users: UserCountsView
    usage: UsageView
    content: ContentCountsView
    reports: ReportCountsView
    top_users: list[TopUserView]

    @classmethod
    def from_stats(cls, stats: SystemStats) -> StatsView:
        return cls(
            users=UserCountsView(
                total=stats.users_total,
                active_7d=stats.users_active_7d,
                disabled=stats.users_disabled,
            ),
            usage=UsageView(
                prompt_tokens=stats.prompt_tokens,
                completion_tokens=stats.completion_tokens,
                total_tokens=stats.total_tokens,
                requests=stats.requests,
            ),
            content=ContentCountsView(
                threads=stats.threads,
                messages=stats.messages,
                projects=stats.projects,
                documents=stats.documents,
                documents_failed=stats.documents_failed,
                memories=stats.memories,
            ),
            reports=ReportCountsView(
                open=stats.reports_open,
                resolved=stats.reports_resolved,
            ),
            top_users=[
                TopUserView(
                    email=user.email,
                    display_name=user.display_name,
                    total_tokens=user.total_tokens,
                    requests=user.requests,
                )
                for user in stats.top_users
            ],
        )


class GrantRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)


class GrantView(StrictModel):
    email: str
    granted_by: str
    created_at: datetime

    @classmethod
    def from_grant(cls, grant: AdminGrant) -> GrantView:
        return cls(
            email=grant.email,
            granted_by=grant.granted_by,
            created_at=grant.created_at,
        )


class AdminListView(StrictModel):
    bootstrap: list[str]
    granted: list[GrantView]


class ReportTurn(StrictModel):
    role: str
    content: str


class AdminReportView(StrictModel):
    report_id: str
    owner_user_id: str
    thread_id: str
    message_position: int | None
    reason: str
    details: str | None
    conversation: list[ReportTurn]
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None

    @classmethod
    def from_record(cls, record: ReportRecord) -> AdminReportView:
        return cls(
            report_id=record.report_id,
            owner_user_id=record.owner_user_id,
            thread_id=record.thread_id,
            message_position=record.message_position,
            reason=record.reason,
            details=record.details,
            conversation=[
                ReportTurn(
                    role=str(turn.get("role", "")),
                    content=str(turn.get("content", "")),
                )
                for turn in record.conversation
            ],
            created_at=record.created_at,
            resolved_at=record.resolved_at,
            resolved_by=record.resolved_by,
        )


class AuditView(StrictModel):
    audit_id: str
    actor_email: str
    action: str
    target: str
    detail: str | None
    created_at: datetime

    @classmethod
    def from_entry(cls, entry: AdminAuditEntry) -> AuditView:
        return cls(
            audit_id=entry.audit_id,
            actor_email=entry.actor_email,
            action=entry.action,
            target=entry.target,
            detail=entry.detail,
            created_at=entry.created_at,
        )


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.get("/admin/me", response_model=AdminMeView)
    def admin_me() -> AdminMeView:
        email = ctx.require_admin()
        return AdminMeView(email=email, bootstrap=email in ctx.admin_emails)

    @app.get("/admin/stats", response_model=StatsView)
    def system_stats() -> StatsView:
        ctx.require_admin()
        return StatsView.from_stats(ctx.admin_store().stats())

    # --- users ---------------------------------------------------------------

    @app.get("/admin/users", response_model=list[AdminUserView])
    def list_users() -> list[AdminUserView]:
        ctx.require_admin()
        return [
            AdminUserView.from_account(account)
            for account in ctx.admin_store().list_users()
        ]

    def _set_disabled(uid: str, *, disabled: bool) -> AdminUserView:
        actor = ctx.require_admin()
        store = ctx.admin_store()
        account = store.get_user(uid)
        if account is None:
            raise HTTPException(status_code=404, detail="User not found")
        if disabled and account.email in ctx.admin_emails:
            raise HTTPException(
                status_code=403,
                detail="Bootstrap administrators cannot be disabled",
            )
        changed = (account.disabled_at is not None) != disabled
        updated = store.set_user_disabled(uid=uid, disabled=disabled)
        if updated is None:
            raise HTTPException(status_code=404, detail="User not found")
        if changed:
            store.record_action(
                actor_email=actor,
                action="disable_user" if disabled else "enable_user",
                target=uid,
                detail=account.email,
            )
        return AdminUserView.from_account(updated)

    @app.post("/admin/users/{uid}/disable", response_model=AdminUserView)
    def disable_user(uid: str) -> AdminUserView:
        return _set_disabled(uid, disabled=True)

    @app.post("/admin/users/{uid}/enable", response_model=AdminUserView)
    def enable_user(uid: str) -> AdminUserView:
        return _set_disabled(uid, disabled=False)

    # --- grants -----------------------------------------------------------------

    @app.get("/admin/admins", response_model=AdminListView)
    def list_admins() -> AdminListView:
        ctx.require_admin()
        return AdminListView(
            bootstrap=list(ctx.admin_emails),
            granted=[
                GrantView.from_grant(grant)
                for grant in ctx.admin_store().list_admins()
            ],
        )

    @app.post(
        "/admin/admins",
        response_model=GrantView,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_admin(request: GrantRequest) -> GrantView:
        actor = ctx.require_admin()
        email = normalize_email(request.email)
        if email in ctx.admin_emails:
            raise HTTPException(
                status_code=409,
                detail="Already an administrator by configuration",
            )
        grant = ctx.admin_store().grant_admin(email=email, granted_by=actor)
        if grant is None:
            raise HTTPException(status_code=409, detail="Already an administrator")
        ctx.admin_store().record_action(
            actor_email=actor, action="grant_admin", target=email
        )
        return GrantView.from_grant(grant)

    @app.delete("/admin/admins/{email}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_admin(email: str) -> None:
        actor = ctx.require_admin()
        normalized = normalize_email(email)
        if normalized in ctx.admin_emails:
            raise HTTPException(
                status_code=403,
                detail="Bootstrap administrators cannot be revoked",
            )
        if not ctx.admin_store().revoke_admin(normalized):
            raise HTTPException(status_code=404, detail="Not an administrator")
        ctx.admin_store().record_action(
            actor_email=actor, action="revoke_admin", target=normalized
        )

    # --- reports -----------------------------------------------------------------

    @app.get("/admin/reports", response_model=list[AdminReportView])
    def list_reports(include_resolved: bool = False) -> list[AdminReportView]:
        ctx.require_admin()
        return [
            AdminReportView.from_record(record)
            for record in ctx.admin_store().list_reports(
                include_resolved=include_resolved
            )
        ]

    @app.post("/admin/reports/{report_id}/resolve", response_model=AdminReportView)
    def resolve_report(report_id: str) -> AdminReportView:
        actor = ctx.require_admin()
        store = ctx.admin_store()
        resolved = store.resolve_report(report_id=report_id, resolved_by=actor)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Report not found")
        record, changed = resolved
        if changed:
            store.record_action(
                actor_email=actor, action="resolve_report", target=report_id
            )
        return AdminReportView.from_record(record)

    # --- audit -------------------------------------------------------------------

    @app.get("/admin/audit", response_model=list[AuditView])
    def list_audit(limit: int = 200) -> list[AuditView]:
        ctx.require_admin()
        return [
            AuditView.from_entry(entry)
            for entry in ctx.admin_store().list_audit(limit=min(max(limit, 1), 1000))
        ]
