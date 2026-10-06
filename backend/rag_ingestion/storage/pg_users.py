from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from rag_ingestion.db.tables import UserRow
from rag_ingestion.storage.threads import normalize_email


class PostgresUserStore:
    """Verified-identity registry in Postgres."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_login(
        self,
        *,
        uid: str,
        email: str | None,
        display_name: str | None,
        clear_email: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.get(UserRow, uid)
            if row is None:
                row = UserRow(
                    uid=uid,
                    # Normalized on the way in so a lookup by address cannot
                    # miss on capitalisation; the registry is what a project
                    # transfer resolves an address against.
                    email=normalize_email(email) if email and not clear_email else None,
                    display_name=display_name,
                    first_login_at=now,
                    last_login_at=now,
                )
            else:
                row.last_login_at = now
                # The provider's profile can change between logins; the token
                # is the source of truth, so newer values win.
                if clear_email:
                    row.email = None
                elif email is not None:
                    row.email = normalize_email(email)
                if display_name is not None:
                    row.display_name = display_name
            session.add(row)
            session.commit()

    def is_registered(self, uid: str) -> bool:
        with Session(self._engine) as session:
            return session.get(UserRow, uid) is not None

    def is_disabled(self, uid: str) -> bool:
        with Session(self._engine) as session:
            row = session.get(UserRow, uid)
            return row is not None and row.disabled_at is not None

    def find_uid_by_email(self, email: str) -> str | None:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            # Two uids can hold one address (relinked providers, a recreated
            # account); the most recent login is the one in use. uid breaks a
            # timestamp tie so the answer never depends on heap order.
            row = session.exec(
                select(UserRow)
                .where(UserRow.email == normalized)
                .order_by(UserRow.last_login_at.desc().nulls_last(), UserRow.uid)
            ).first()
            return row.uid if row is not None else None

    def close(self) -> None:
        self._engine.dispose()
