from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from datetime import timedelta

from sqlalchemy import func

from rag_ingestion.db.tables import (
    AdminAuditRow,
    AdminUserRow,
    LlmUsageRow,
    MemoryRow,
    MessageReportRow,
    ProjectFileRow,
    ProjectRow,
    ThreadMessageRow,
    ThreadRow,
    UserRow,
)
from rag_ingestion.storage.admin import (
    AdminAuditEntry,
    AdminGrant,
    AdminUserAccount,
    ReportRecord,
    SystemStats,
    TopUser,
)
from rag_ingestion.storage.threads import normalize_email


def _account(user: UserRow, usage: LlmUsageRow | None) -> AdminUserAccount:
    return AdminUserAccount(
        uid=user.uid,
        email=user.email,
        display_name=user.display_name,
        first_login_at=user.first_login_at,
        last_login_at=user.last_login_at,
        disabled_at=user.disabled_at,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        requests=usage.requests if usage else 0,
    )


def _report(row: MessageReportRow) -> ReportRecord:
    return ReportRecord(
        report_id=row.report_id,
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        message_position=row.message_position,
        reason=row.reason,
        details=row.details,
        conversation=json.loads(row.conversation),
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
    )


class PostgresAdminStore:
    """The admin view over Postgres."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- users -----------------------------------------------------------------

    def list_users(self) -> list[AdminUserAccount]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(UserRow, LlmUsageRow)
                .join(
                    LlmUsageRow,
                    LlmUsageRow.owner_user_id == UserRow.uid,  # type: ignore[arg-type]
                    isouter=True,
                )
                .order_by(UserRow.last_login_at.desc())  # type: ignore[attr-defined]
            ).all()
            return [_account(user, usage) for user, usage in rows]

    def stats(self) -> SystemStats:
        week_ago = datetime.now(UTC) - timedelta(days=7)
        with Session(self._engine) as session:
            def count(query) -> int:
                return int(session.exec(query).one())

            usage = session.exec(
                select(
                    func.coalesce(func.sum(LlmUsageRow.prompt_tokens), 0),
                    func.coalesce(func.sum(LlmUsageRow.completion_tokens), 0),
                    func.coalesce(func.sum(LlmUsageRow.total_tokens), 0),
                    func.coalesce(func.sum(LlmUsageRow.requests), 0),
                )
            ).one()
            top = session.exec(
                select(UserRow.email, UserRow.display_name, LlmUsageRow)
                .join(LlmUsageRow, LlmUsageRow.owner_user_id == UserRow.uid)  # type: ignore[arg-type]
                .where(LlmUsageRow.total_tokens > 0)
                .order_by(LlmUsageRow.total_tokens.desc())  # type: ignore[attr-defined]
                .limit(5)
            ).all()
            return SystemStats(
                users_total=count(select(func.count()).select_from(UserRow)),
                users_active_7d=count(
                    select(func.count())
                    .select_from(UserRow)
                    .where(UserRow.last_login_at >= week_ago)
                ),
                users_disabled=count(
                    select(func.count())
                    .select_from(UserRow)
                    .where(UserRow.disabled_at.is_not(None))  # type: ignore[union-attr]
                ),
                prompt_tokens=int(usage[0]),
                completion_tokens=int(usage[1]),
                total_tokens=int(usage[2]),
                requests=int(usage[3]),
                threads=count(select(func.count()).select_from(ThreadRow)),
                messages=count(select(func.count()).select_from(ThreadMessageRow)),
                projects=count(select(func.count()).select_from(ProjectRow)),
                documents=count(
                    select(func.count())
                    .select_from(ProjectFileRow)
                    .where(ProjectFileRow.trashed_at.is_(None))  # type: ignore[union-attr]
                ),
                documents_failed=count(
                    select(func.count())
                    .select_from(ProjectFileRow)
                    .where(ProjectFileRow.status == "failed")
                ),
                memories=count(select(func.count()).select_from(MemoryRow)),
                reports_open=count(
                    select(func.count())
                    .select_from(MessageReportRow)
                    .where(MessageReportRow.resolved_at.is_(None))  # type: ignore[union-attr]
                ),
                reports_resolved=count(
                    select(func.count())
                    .select_from(MessageReportRow)
                    .where(MessageReportRow.resolved_at.is_not(None))  # type: ignore[union-attr]
                ),
                top_users=[
                    TopUser(
                        email=email,
                        display_name=name,
                        total_tokens=row.total_tokens,
                        requests=row.requests,
                    )
                    for email, name, row in top
                ],
            )

    def get_user(self, uid: str) -> AdminUserAccount | None:
        with Session(self._engine) as session:
            user = session.get(UserRow, uid)
            if user is None:
                return None
            return _account(user, session.get(LlmUsageRow, uid))

    def set_user_disabled(
        self, *, uid: str, disabled: bool
    ) -> AdminUserAccount | None:
        with Session(self._engine) as session:
            user = session.get(UserRow, uid)
            if user is None:
                return None
            user.disabled_at = datetime.now(UTC) if disabled else None
            session.add(user)
            session.commit()
            session.refresh(user)
            return _account(user, session.get(LlmUsageRow, uid))

    # --- grants ----------------------------------------------------------------

    def is_admin(self, email: str) -> bool:
        with Session(self._engine) as session:
            return session.get(AdminUserRow, normalize_email(email)) is not None

    def list_admins(self) -> list[AdminGrant]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(AdminUserRow).order_by(AdminUserRow.created_at)  # type: ignore[arg-type]
            ).all()
            return [
                AdminGrant(
                    email=row.email,
                    granted_by=row.granted_by,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def grant_admin(self, *, email: str, granted_by: str) -> AdminGrant | None:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            if session.get(AdminUserRow, normalized) is not None:
                return None
            row = AdminUserRow(
                email=normalized,
                granted_by=normalize_email(granted_by),
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.commit()
            return AdminGrant(
                email=row.email, granted_by=row.granted_by, created_at=row.created_at
            )

    def revoke_admin(self, email: str) -> bool:
        with Session(self._engine) as session:
            row = session.get(AdminUserRow, normalize_email(email))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # --- reports ----------------------------------------------------------------

    def list_reports(self, *, include_resolved: bool = False) -> list[ReportRecord]:
        with Session(self._engine) as session:
            query = select(MessageReportRow)
            if not include_resolved:
                query = query.where(MessageReportRow.resolved_at.is_(None))  # type: ignore[union-attr]
            query = query.order_by(MessageReportRow.created_at.desc())  # type: ignore[attr-defined]
            return [_report(row) for row in session.exec(query).all()]

    def resolve_report(
        self, *, report_id: str, resolved_by: str
    ) -> tuple[ReportRecord, bool] | None:
        with Session(self._engine) as session:
            row = session.get(MessageReportRow, report_id)
            if row is None:
                return None
            if row.resolved_at is not None:
                return _report(row), False
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = normalize_email(resolved_by)
            session.add(row)
            session.commit()
            session.refresh(row)
            return _report(row), True

    # --- audit -------------------------------------------------------------------

    def record_action(
        self, *, actor_email: str, action: str, target: str, detail: str | None = None
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                AdminAuditRow(
                    audit_id=f"aud_{uuid.uuid4().hex}",
                    actor_email=normalize_email(actor_email),
                    action=action,
                    target=target,
                    detail=detail,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()

    def list_audit(self, *, limit: int = 200) -> list[AdminAuditEntry]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(AdminAuditRow)
                .order_by(AdminAuditRow.created_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            ).all()
            return [
                AdminAuditEntry(
                    audit_id=row.audit_id,
                    actor_email=row.actor_email,
                    action=row.action,
                    target=row.target,
                    detail=row.detail,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def close(self) -> None:
        self._engine.dispose()
