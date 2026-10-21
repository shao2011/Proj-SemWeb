from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, func
from sqlmodel import Session, col, delete, select

from rag_ingestion.db.tables import (
    THREAD_RECENCY_SEQUENCE,
    MessageReportRow,
    ThreadMessageRow,
    ThreadRow,
    ThreadShareRecipientRow,
    ThreadShareRow,
)
from rag_ingestion.storage.threads import (
    Citation,
    ShareAccess,
    Thread,
    ThreadMessage,
    ThreadMessageRole,
    ThreadShare,
    normalize_email,
)


class PostgresThreadStore:
    """Conversation history in Postgres.

    Returns the same frozen Pydantic contracts as the previous store, so the
    routes and their response models are unchanged.

    Two counters are held here rather than leaned on from the database:
    `sequence` orders the recents list (a timestamp is unusable, some hosts
    resolve the clock coarsely enough that several writes share one value), and
    `position` orders messages within a thread (Postgres has no implicit rowid
    to fall back on). Both are read with a row lock so two concurrent writers
    cannot claim the same number.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_thread(
        self,
        *,
        owner_user_id: str,
        title: str,
        project_id: str | None = None,
    ) -> Thread:
        now = datetime.now(UTC)
        thread = Thread(
            thread_id=f"thr_{uuid.uuid4().hex}",
            owner_user_id=owner_user_id,
            project_id=project_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        with Session(self._engine) as session:
            session.add(
                ThreadRow(
                    thread_id=thread.thread_id,
                    owner_user_id=thread.owner_user_id,
                    project_id=thread.project_id,
                    title=thread.title,
                    created_at=thread.created_at,
                    updated_at=thread.updated_at,
                    sequence=self._next_sequence(session),
                )
            )
            session.commit()
        return thread

    def get_thread(self, *, owner_user_id: str, thread_id: str) -> Thread | None:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id)
            return self._thread(row) if row is not None else None

    def list_threads(self, *, owner_user_id: str, limit: int = 50) -> list[Thread]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(ThreadRow)
                .where(ThreadRow.owner_user_id == owner_user_id)
                .order_by(col(ThreadRow.sequence).desc())
                .limit(limit)
            ).all()
            return [self._thread(row) for row in rows]

    def rename_thread(
        self, *, owner_user_id: str, thread_id: str, title: str
    ) -> Thread | None:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id)
            if row is None:
                return None
            # `sequence` is deliberately left alone: renaming is not activity and
            # should not move the thread to the top of the recents list.
            row.title = title
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._thread(row)

    def count_user_messages(self, *, owner_user_id: str) -> int:
        """Total user-role messages across every thread this identity owns.

        Backs the anonymous trial limit, so it deliberately spans threads:
        opening a new chat must not reset the allowance.
        """
        with Session(self._engine) as session:
            total = session.exec(
                select(func.count(col(ThreadMessageRow.message_id)))
                .join(ThreadRow, col(ThreadMessageRow.thread_id) == col(ThreadRow.thread_id))
                .where(
                    ThreadRow.owner_user_id == owner_user_id,
                    ThreadMessageRow.role == "user",
                )
            ).one()
            return int(total)

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int:
        """Move every thread of `anonymous_user_id` to `verified_user_id`.

        Messages hang off the thread row, so only `threads` carries the owner.
        """
        with Session(self._engine) as session:
            rows = session.exec(
                select(ThreadRow).where(
                    ThreadRow.owner_user_id == anonymous_user_id
                )
            ).all()
            for row in rows:
                row.owner_user_id = verified_user_id
                session.add(row)
            session.commit()
            return len(rows)

    def delete_thread(self, *, owner_user_id: str, thread_id: str) -> bool:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id)
            if row is None:
                return False
            # Explicit child delete: the ORM does not emit the database-level
            # ON DELETE CASCADE unless the parent's children are loaded.
            session.exec(
                delete(ThreadMessageRow).where(
                    col(ThreadMessageRow.thread_id) == thread_id
                )
            )
            session.delete(row)
            session.commit()
            return True

    def set_summary(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        summary: str,
        summarized_through_position: int,
    ) -> Thread | None:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id)
            if row is None:
                return None
            # `sequence` and `updated_at` are both left alone, for the reason
            # `rename_thread` leaves `sequence` alone: this runs after the answer
            # has already been sent, so it is bookkeeping, not activity, and must
            # not reorder the recents list for something the user never did.
            row.summary = summary
            row.summarized_through_position = summarized_through_position
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._thread(row)

    def record_prompt_metrics(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        prompt_tokens: int,
        prompt_chars: int,
        model_id: str,
    ) -> Thread | None:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id)
            if row is None:
                return None
            # Same contract as set_summary: bookkeeping, so `sequence` and
            # `updated_at` stay untouched and the recents order holds.
            row.last_prompt_tokens = prompt_tokens
            row.last_prompt_chars = prompt_chars
            row.last_model_id = model_id
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._thread(row)

    def add_message(
        self,
        *,
        owner_user_id: str,
        thread_id: str,
        role: ThreadMessageRole,
        content: str,
        citations: list[Citation] | None = None,
    ) -> ThreadMessage | None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, thread_id, for_update=True)
            if row is None:
                return None
            # Built inside the transaction so the returned contract carries the
            # position the row actually got, which is what a caller needs to
            # place the message against the summary watermark.
            message = ThreadMessage(
                message_id=f"msg_{uuid.uuid4().hex}",
                thread_id=thread_id,
                role=role,
                content=content,
                citations=list(citations or []),
                created_at=now,
                position=self._next_position(session, thread_id),
            )
            session.add(
                ThreadMessageRow(
                    message_id=message.message_id,
                    thread_id=thread_id,
                    role=message.role.value,
                    content=message.content,
                    citations=json.dumps(
                        [citation.model_dump() for citation in message.citations],
                        separators=(",", ":"),
                    ),
                    created_at=message.created_at,
                    position=message.position,
                )
            )
            row.updated_at = message.created_at
            row.sequence = self._next_sequence(session)
            session.add(row)
            session.commit()
        return message

    def list_messages(
        self, *, owner_user_id: str, thread_id: str, limit: int = 200
    ) -> list[ThreadMessage] | None:
        """The newest `limit` messages, returned in conversation order.

        Ordered descending under the LIMIT and reversed after, not ascending:
        ascending gave the 200 *oldest* messages, so once a thread passed the
        limit every later turn was answered against the opening of the
        conversation and the recent turns it needed were the ones dropped.

        The slice therefore no longer starts at position 1 on a long thread.
        Callers that need the opening turn, or the span the summary has not
        covered yet, ask for it by position through `list_messages_range`.
        """
        with Session(self._engine) as session:
            if self._owned(session, owner_user_id, thread_id) is None:
                return None
            rows = session.exec(
                select(ThreadMessageRow)
                .where(ThreadMessageRow.thread_id == thread_id)
                .order_by(col(ThreadMessageRow.position).desc())
                .limit(limit)
            ).all()
            return [self._message(row) for row in reversed(rows)]

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

        An inverted or empty range returns `[]` rather than raising: the
        summarizer reaches its watermark by having nothing left to fold, which
        is a normal state and not a fault.
        """
        with Session(self._engine) as session:
            if self._owned(session, owner_user_id, thread_id) is None:
                return None
            if through_position < from_position:
                return []
            rows = session.exec(
                select(ThreadMessageRow)
                .where(
                    ThreadMessageRow.thread_id == thread_id,
                    col(ThreadMessageRow.position) >= from_position,
                    col(ThreadMessageRow.position) <= through_position,
                )
                .order_by(col(ThreadMessageRow.position))
                .limit(limit)
            ).all()
            return [self._message(row) for row in rows]

    def count_messages(self, *, owner_user_id: str, thread_id: str) -> int | None:
        """Total messages in the thread.

        Needed because `len(list_messages(...))` saturates at its limit, and the
        summary watermark is computed from the thread's real length.
        """
        with Session(self._engine) as session:
            if self._owned(session, owner_user_id, thread_id) is None:
                return None
            return int(
                session.exec(
                    select(func.count()).where(
                        col(ThreadMessageRow.thread_id) == thread_id
                    )
                ).one()
                or 0
            )

    def fork_thread(
        self, *, owner_user_id: str, thread_id: str, through_position: int, title: str
    ) -> Thread | None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            source = self._owned(session, owner_user_id, thread_id)
            if source is None:
                return None
            rows = session.exec(
                select(ThreadMessageRow)
                .where(
                    ThreadMessageRow.thread_id == thread_id,
                    col(ThreadMessageRow.position) <= through_position,
                )
                .order_by(col(ThreadMessageRow.position))
            ).all()
            forked = Thread(
                thread_id=f"thr_{uuid.uuid4().hex}",
                owner_user_id=owner_user_id,
                project_id=source.project_id,
                title=title,
                created_at=now,
                updated_at=now,
            )
            session.add(
                ThreadRow(
                    thread_id=forked.thread_id,
                    owner_user_id=forked.owner_user_id,
                    project_id=forked.project_id,
                    title=forked.title,
                    created_at=now,
                    updated_at=now,
                    sequence=self._next_sequence(session),
                )
            )
            # Flushed before the messages: they carry a foreign key to a thread
            # row that does not exist on the database side until this runs.
            session.flush()
            # Renumbered from 1: the copy is its own conversation, and a gap
            # in positions would break the window and the summary watermark.
            # `created_at` is kept, because the turns really did happen then.
            for position, row in enumerate(rows, start=1):
                session.add(
                    ThreadMessageRow(
                        message_id=f"msg_{uuid.uuid4().hex}",
                        thread_id=forked.thread_id,
                        role=row.role,
                        content=row.content,
                        citations=row.citations,
                        created_at=row.created_at,
                        position=position,
                    )
                )
            session.commit()
        return forked

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
        report_id = f"rpt_{uuid.uuid4().hex}"
        with Session(self._engine) as session:
            session.add(
                MessageReportRow(
                    report_id=report_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    message_position=message_position,
                    reason=reason,
                    details=details,
                    conversation=json.dumps(conversation, ensure_ascii=False),
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        return report_id

    # --- sharing ---------------------------------------------------------------

    def share_thread(
        self, *, owner_user_id: str, thread_id: str, title: str, through_position: int
    ) -> ThreadShare:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            existing = session.exec(
                select(ThreadShareRow).where(ThreadShareRow.thread_id == thread_id)
            ).first()
            if existing is not None:
                return self._share(
                    existing, self._recipients(session, existing.share_id)
                )
            row = ThreadShareRow(
                share_id=f"shr_{uuid.uuid4().hex}",
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                title=title,
                shared_through_position=through_position,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._share(row, [])

    def refresh_share(
        self, *, owner_user_id: str, thread_id: str, title: str, through_position: int
    ) -> ThreadShare | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(ThreadShareRow).where(
                    ThreadShareRow.thread_id == thread_id,
                    ThreadShareRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return None
            row.title = title
            row.shared_through_position = through_position
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._share(row, self._recipients(session, row.share_id))

    def get_thread_share(
        self, *, owner_user_id: str, thread_id: str
    ) -> ThreadShare | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(ThreadShareRow).where(
                    ThreadShareRow.thread_id == thread_id,
                    ThreadShareRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return None
            return self._share(row, self._recipients(session, row.share_id))

    def get_share(self, *, share_id: str) -> ThreadShare | None:
        with Session(self._engine) as session:
            row = session.get(ThreadShareRow, share_id)
            if row is None:
                return None
            return self._share(row, self._recipients(session, share_id))

    def revoke_share(self, *, owner_user_id: str, thread_id: str) -> bool:
        with Session(self._engine) as session:
            row = session.exec(
                select(ThreadShareRow).where(
                    ThreadShareRow.thread_id == thread_id,
                    ThreadShareRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return False
            # Deleted, not flagged: a revoked link must not be resurrectable,
            # and the next share deliberately mints a different token.
            session.delete(row)
            session.commit()
            return True

    def set_share_access(
        self, *, owner_user_id: str, thread_id: str, access: ShareAccess
    ) -> ThreadShare | None:
        with Session(self._engine) as session:
            row = self._share_row(session, owner_user_id, thread_id)
            if row is None:
                return None
            row.access = access.value
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._share(row, self._recipients(session, row.share_id))

    def add_share_recipient(
        self, *, owner_user_id: str, thread_id: str, email: str
    ) -> ThreadShare | None:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            row = self._share_row(session, owner_user_id, thread_id)
            if row is None:
                return None
            existing = session.exec(
                select(ThreadShareRecipientRow).where(
                    ThreadShareRecipientRow.share_id == row.share_id,
                    ThreadShareRecipientRow.email == normalized,
                )
            ).first()
            if existing is None:
                session.add(
                    ThreadShareRecipientRow(
                        recipient_id=f"shr_rcp_{uuid.uuid4().hex}",
                        share_id=row.share_id,
                        email=normalized,
                        created_at=datetime.now(UTC),
                    )
                )
            # Naming someone is the act that closes the link. Doing it here
            # rather than leaving it to the caller is what makes "only that
            # address can open it" true the moment the owner says so.
            row.access = ShareAccess.INVITE.value
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._share(row, self._recipients(session, row.share_id))

    def remove_share_recipient(
        self, *, owner_user_id: str, thread_id: str, email: str
    ) -> ThreadShare | None:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            row = self._share_row(session, owner_user_id, thread_id)
            if row is None:
                return None
            session.exec(
                delete(ThreadShareRecipientRow).where(
                    col(ThreadShareRecipientRow.share_id) == row.share_id,
                    col(ThreadShareRecipientRow.email) == normalized,
                )
            )
            session.commit()
            # The access mode is deliberately left alone: emptying the list
            # must not silently reopen the chat to the whole internet.
            session.refresh(row)
            return self._share(row, self._recipients(session, row.share_id))

    def list_shares_for_email(self, *, email: str) -> list[ThreadShare]:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            rows = session.exec(
                select(ThreadShareRow)
                .join(
                    ThreadShareRecipientRow,
                    col(ThreadShareRecipientRow.share_id) == col(ThreadShareRow.share_id),
                )
                .where(
                    ThreadShareRecipientRow.email == normalized,
                    ThreadShareRow.access == ShareAccess.INVITE.value,
                )
                .order_by(col(ThreadShareRow.updated_at).desc())
            ).all()
            return [
                self._share(row, self._recipients(session, row.share_id))
                for row in rows
            ]

    def fork_share(
        self, *, share: ThreadShare, new_owner_user_id: str
    ) -> Thread | None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            rows = session.exec(
                select(ThreadMessageRow)
                .where(
                    ThreadMessageRow.thread_id == share.thread_id,
                    col(ThreadMessageRow.position) <= share.shared_through_position,
                )
                .order_by(col(ThreadMessageRow.position))
            ).all()
            forked = Thread(
                thread_id=f"thr_{uuid.uuid4().hex}",
                owner_user_id=new_owner_user_id,
                # Deliberately no project: the reader must not be able to
                # retrieve over the owner's documents, and belonging to no
                # project is what makes that structural rather than a check.
                project_id=None,
                title=share.title,
                created_at=now,
                updated_at=now,
            )
            session.add(
                ThreadRow(
                    thread_id=forked.thread_id,
                    owner_user_id=forked.owner_user_id,
                    project_id=None,
                    title=forked.title,
                    created_at=now,
                    updated_at=now,
                    sequence=self._next_sequence(session),
                )
            )
            session.flush()
            for position, row in enumerate(rows, start=1):
                session.add(
                    ThreadMessageRow(
                        message_id=f"msg_{uuid.uuid4().hex}",
                        thread_id=forked.thread_id,
                        role=row.role,
                        content=row.content,
                        # Dropped: chunk and document ids describe the owner's
                        # library, and they would point at documents this
                        # reader cannot open anyway.
                        citations="[]",
                        created_at=row.created_at,
                        position=position,
                    )
                )
            session.commit()
        return forked

    def close(self) -> None:
        self._engine.dispose()

    @staticmethod
    def _share_row(
        session: Session, owner_user_id: str, thread_id: str
    ) -> ThreadShareRow | None:
        return session.exec(
            select(ThreadShareRow).where(
                ThreadShareRow.thread_id == thread_id,
                ThreadShareRow.owner_user_id == owner_user_id,
            )
        ).first()

    @staticmethod
    def _recipients(session: Session, share_id: str) -> list[str]:
        return list(
            session.exec(
                select(ThreadShareRecipientRow.email)
                .where(ThreadShareRecipientRow.share_id == share_id)
                .order_by(col(ThreadShareRecipientRow.created_at))
            ).all()
        )

    @staticmethod
    def _share(row: ThreadShareRow, recipients: list[str] | None = None) -> ThreadShare:
        return ThreadShare(
            share_id=row.share_id,
            thread_id=row.thread_id,
            owner_user_id=row.owner_user_id,
            title=row.title,
            shared_through_position=row.shared_through_position,
            access=ShareAccess(row.access),
            recipients=recipients or [],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _owned(
        session: Session,
        owner_user_id: str,
        thread_id: str,
        *,
        for_update: bool = False,
    ) -> ThreadRow | None:
        statement = select(ThreadRow).where(
            ThreadRow.thread_id == thread_id,
            ThreadRow.owner_user_id == owner_user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return session.exec(statement).first()

    @staticmethod
    def _next_sequence(session: Session) -> int:
        """One number from the global counter, never handed out twice.

        A database sequence, not `max(sequence) + 1`: that read-then-write was
        not atomic across transactions, so two threads touched at the same
        moment both read the same maximum and both wrote it, leaving the recents
        order between them to the execution plan. `nextval` is atomic and does
        not wait on other writers.
        """
        return int(session.exec(select(THREAD_RECENCY_SEQUENCE.next_value())).one())

    @staticmethod
    def _next_position(session: Session, thread_id: str) -> int:
        current = session.exec(
            select(func.max(col(ThreadMessageRow.position))).where(
                col(ThreadMessageRow.thread_id) == thread_id
            )
        ).one()
        return int(current or 0) + 1

    @staticmethod
    def _thread(row: ThreadRow) -> Thread:
        return Thread(
            thread_id=row.thread_id,
            owner_user_id=row.owner_user_id,
            project_id=row.project_id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            summary=row.summary,
            summarized_through_position=row.summarized_through_position,
            last_prompt_tokens=row.last_prompt_tokens,
            last_prompt_chars=row.last_prompt_chars,
            last_model_id=row.last_model_id,
        )

    @staticmethod
    def _message(row: ThreadMessageRow) -> ThreadMessage:
        return ThreadMessage(
            message_id=row.message_id,
            thread_id=row.thread_id,
            role=ThreadMessageRole(row.role),
            content=row.content,
            citations=json.loads(row.citations),
            created_at=row.created_at,
            position=row.position,
        )
