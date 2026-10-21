from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, func, or_
from sqlmodel import Session, col, delete, select

from rag_ingestion.db.tables import (
    LlmUsageRow,
    MemoryAuditRow,
    MemoryProposalRow,
    MemoryRow,
    UserSettingsRow,
)
from rag_ingestion.logger import get_logger
from rag_ingestion.memory.models import (
    MEMORY_CAPS,
    LlmUsage,
    Memory,
    MemoryAuditEntry,
    MemoryOperation,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    MemoryType,
    UserSettings,
)

_log = get_logger(__name__)


class PostgresMemoryStore:
    """Memory storage: three scopes, four operations, per-scope caps.

    Two rules from the design carry weight here. Deletes are marks, never row
    removals, so a bad write stays auditable and reviewable. And adding a
    memory whose `key` already exists in the same scope supersedes the previous
    one rather than sitting beside it, which is what keeps two contradictory
    values from coexisting.

    Reading these rows into the prompt happens elsewhere: `api.routes` recalls
    them per turn and `retrieval.answering.build_turns` renders them.
    """

    def __init__(
        self, engine: Engine, *, caps: dict[MemoryScope, int] | None = None
    ) -> None:
        self._engine = engine
        # Defaults keep every existing caller and test working unchanged;
        # `api.app` passes the configured values.
        self._caps = dict(caps) if caps else dict(MEMORY_CAPS)

    # --- memory -------------------------------------------------------------

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
        source_message_id: str | None = None,
    ) -> Memory:
        now = datetime.now(UTC)
        # Validated before touching the database so an invalid key or a scope
        # without its target never reaches Postgres.
        memory = Memory(
            memory_id=f"mem_{uuid.uuid4().hex}",
            owner_user_id=owner_user_id,
            scope=scope,
            thread_id=thread_id,
            project_id=project_id,
            type=type,
            key=str(key),
            value=value,
            source=source,
            created_at=now,
            updated_at=now,
        )
        with Session(self._engine) as session:
            superseded = self._claim_slot(session, memory, now=now)
            session.add(self._to_row(memory))
            session.add(
                self._audit(
                    session,
                    memory_id=memory.memory_id,
                    op=MemoryOperation.ADD,
                    source=memory.source.value,
                    created_at=memory.created_at,
                    source_message_id=source_message_id,
                )
            )
            session.commit()
        # After the commit, so the log never claims a write that rolled back.
        _log.info(
            "Memory ADD %s %s source=%s%s",
            memory.memory_id,
            self._describe(memory.scope, memory.type.value, memory.key, memory.thread_id, memory.project_id),
            memory.source.value,
            f" superseded={','.join(superseded)}" if superseded else "",
        )
        return memory

    def get_memory(
        self, *, owner_user_id: str, memory_id: str, include_deleted: bool = False
    ) -> Memory | None:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, memory_id, include_deleted)
            return self._to_memory(row) if row is not None else None

    def list_memories(
        self,
        *,
        owner_user_id: str,
        scope: MemoryScope | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        statement = select(MemoryRow).where(
            MemoryRow.owner_user_id == owner_user_id,
            col(MemoryRow.deleted_at).is_(None),
        )
        if scope is not None:
            statement = statement.where(MemoryRow.scope == scope.value)
        if thread_id is not None:
            statement = statement.where(MemoryRow.thread_id == thread_id)
        if project_id is not None:
            statement = statement.where(MemoryRow.project_id == project_id)
        statement = (
            statement.order_by(col(MemoryRow.created_at).desc(), col(MemoryRow.memory_id))
            .offset(offset)
            .limit(limit)
        )
        with Session(self._engine) as session:
            return [self._to_memory(row) for row in session.exec(statement).all()]

    def recall(
        self,
        *,
        owner_user_id: str,
        thread_id: str | None = None,
        project_id: str | None = None,
    ) -> list[Memory]:
        """Everything that applies to one turn, in one query.

        A union, not a filter, which is what separates this from
        `list_memories`: user-scoped rows have a NULL `thread_id`, so filtering
        on the thread would drop exactly the global memories the prompt needs.
        Both scopes come back together and the caller resolves which wins per
        key.
        """
        applies = [MemoryRow.scope == MemoryScope.USER.value]
        if thread_id is not None:
            applies.append(
                and_(
                    MemoryRow.scope == MemoryScope.THREAD.value,
                    MemoryRow.thread_id == thread_id,
                )
            )
        if project_id is not None:
            applies.append(
                and_(
                    MemoryRow.scope == MemoryScope.PROJECT.value,
                    MemoryRow.project_id == project_id,
                )
            )
        with Session(self._engine) as session:
            rows = session.exec(
                select(MemoryRow)
                .where(
                    MemoryRow.owner_user_id == owner_user_id,
                    col(MemoryRow.deleted_at).is_(None),
                    or_(*applies),
                )
                .order_by(col(MemoryRow.key), col(MemoryRow.memory_id))
            ).all()
            return [self._to_memory(row) for row in rows]

    def update_memory(
        self,
        *,
        owner_user_id: str,
        memory_id: str,
        value: str | None = None,
        promote_to_user: bool = False,
    ) -> Memory | None:
        now = datetime.now(UTC)
        superseded: list[str] = []
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, memory_id, include_deleted=False)
            if row is None:
                return None
            # The post-change contract is built before the row is touched, and it
            # is what validates the change: a promotion that would produce a row
            # the model rejects on read has to fail here, not on the next read.
            # Building it first also keeps the row clean while `_claim_slot`
            # queries, since an autoflush of the promoted row ahead of the
            # supersede would collide with it on the live-key index.
            updated = self._to_memory(row).model_copy(
                update={
                    "value": row.value if value is None else value,
                    "scope": MemoryScope.USER if promote_to_user else MemoryScope(row.scope),
                    "thread_id": None if promote_to_user else row.thread_id,
                    "project_id": None if promote_to_user else row.project_id,
                    "updated_at": now,
                }
            )
            Memory.model_validate(updated.model_dump())
            if promote_to_user:
                # Promotion is a write into the user scope, so it answers to the
                # same rules as one: it only moved `scope` before, which left the
                # older global value for this key live beside it and the cap
                # unchecked. `exclude` keeps the row being promoted out of its own
                # cap count and out of its own supersede pass.
                superseded = self._claim_slot(
                    session, updated, now=now, exclude=memory_id
                )
            if value is not None:
                row.value = value
            if promote_to_user:
                row.scope = MemoryScope.USER.value
                row.thread_id = None
                row.project_id = None
            row.updated_at = now
            session.add(row)
            session.add(
                self._audit(
                    session,
                    memory_id=memory_id,
                    op=MemoryOperation.UPDATE,
                    source=row.source,
                    created_at=row.updated_at,
                )
            )
            session.commit()
        changes = []
        if value is not None:
            changes.append("value")
        if promote_to_user:
            changes.append("promoted to user")
        _log.info(
            "Memory UPDATE %s %s changed=%s%s",
            memory_id,
            self._describe(
                updated.scope,
                updated.type.value,
                updated.key,
                updated.thread_id,
                updated.project_id,
            ),
            "+".join(changes),
            f" superseded={','.join(superseded)}" if superseded else "",
        )
        return updated

    def delete_memory(self, *, owner_user_id: str, memory_id: str) -> bool:
        with Session(self._engine) as session:
            row = self._owned(session, owner_user_id, memory_id, include_deleted=False)
            if row is None:
                return False
            now = datetime.now(UTC)
            row.deleted_at = now
            row.updated_at = now
            session.add(row)
            session.add(
                self._audit(
                    session,
                    memory_id=memory_id,
                    op=MemoryOperation.DELETE,
                    source=row.source,
                    created_at=now,
                )
            )
            described = self._describe(
                MemoryScope(row.scope), row.type, row.key, row.thread_id, row.project_id
            )
            session.commit()
        # "marked", not "removed": the row is still there with deleted_at set.
        _log.info("Memory DELETE %s %s marked", memory_id, described)
        return True

    def distinct_thread_count(self, *, owner_user_id: str, key: str) -> int:
        """Separate threads holding a live thread-scoped memory on `key`.

        Live only: a row the owner deleted is them saying no, and it should not
        count towards promoting the same preference to every conversation.
        """
        with Session(self._engine) as session:
            return int(
                session.exec(
                    select(func.count(func.distinct(col(MemoryRow.thread_id)))).where(
                        MemoryRow.owner_user_id == owner_user_id,
                        MemoryRow.key == key,
                        MemoryRow.scope == MemoryScope.THREAD.value,
                        col(MemoryRow.deleted_at).is_(None),
                    )
                ).one()
                or 0
            )

    def audit_trail(self, *, memory_id: str) -> list[MemoryAuditEntry]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(MemoryAuditRow)
                .where(MemoryAuditRow.memory_id == memory_id)
                .order_by(col(MemoryAuditRow.sequence))
            ).all()
            # Mapped before the session closes: a detached ORM row raises on
            # attribute access.
            return [
                MemoryAuditEntry(
                    audit_id=row.audit_id,
                    memory_id=row.memory_id,
                    op=MemoryOperation(row.op),
                    source=MemorySource(row.source),
                    source_message_id=row.source_message_id,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    # --- proposals ----------------------------------------------------------

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
    ) -> MemoryProposal:
        proposal = MemoryProposal(
            proposal_id=f"prop_{uuid.uuid4().hex}",
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            suggested_scope=suggested_scope,
            type=type,
            key=str(key),
            value=value,
            source_message_id=source_message_id,
            created_at=datetime.now(UTC),
        )
        with Session(self._engine) as session:
            session.add(
                MemoryProposalRow(
                    proposal_id=proposal.proposal_id,
                    owner_user_id=proposal.owner_user_id,
                    thread_id=proposal.thread_id,
                    suggested_scope=proposal.suggested_scope.value,
                    type=proposal.type.value,
                    key=proposal.key,
                    value=proposal.value,
                    source_message_id=proposal.source_message_id,
                    created_at=proposal.created_at,
                )
            )
            session.commit()
        # Logged because the alternative reads as a lost write: the route reports
        # "extracted N candidate(s)" and the memory table stays empty, since a
        # non-thread candidate waits here for the user to confirm it.
        _log.info(
            "Memory PROPOSE %s suggested_scope=%s type=%s key=%s thread=%s",
            proposal.proposal_id,
            proposal.suggested_scope.value,
            proposal.type.value,
            proposal.key,
            proposal.thread_id,
        )
        return proposal

    def list_proposals(
        self, *, owner_user_id: str, limit: int = 50
    ) -> list[MemoryProposal]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(MemoryProposalRow)
                .where(MemoryProposalRow.owner_user_id == owner_user_id)
                .order_by(col(MemoryProposalRow.created_at), col(MemoryProposalRow.proposal_id))
                .limit(limit)
            ).all()
            return [self._to_proposal(row) for row in rows]

    def confirm_proposal(
        self, *, owner_user_id: str, proposal_id: str
    ) -> Memory | None:
        """Turn a proposal into a memory, or into nothing at all.

        One transaction, with the proposal row locked for the duration. It used
        to run as three: read the proposal, write the memory through
        `add_memory`, then dismiss. Two confirms arriving together could both
        read the same proposal and write two memories for it, and a crash
        between the second and third left a proposal that had already been
        acted on still sitting in the list.

        Returns `None` when there is nothing to confirm, which covers both an
        unknown proposal and one a concurrent confirm has already taken.
        """
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.exec(
                select(MemoryProposalRow)
                .where(
                    MemoryProposalRow.proposal_id == proposal_id,
                    MemoryProposalRow.owner_user_id == owner_user_id,
                )
                .with_for_update()
            ).first()
            if row is None:
                return None
            proposal = self._to_proposal(row)
            memory = Memory(
                memory_id=f"mem_{uuid.uuid4().hex}",
                owner_user_id=owner_user_id,
                scope=proposal.suggested_scope,
                thread_id=(
                    proposal.thread_id
                    if proposal.suggested_scope is MemoryScope.THREAD
                    else None
                ),
                project_id=None,
                type=proposal.type,
                key=proposal.key,
                value=proposal.value,
                source=MemorySource.USER_CONFIRMED,
                created_at=now,
                updated_at=now,
            )
            superseded = self._claim_slot(session, memory, now=now)
            session.add(self._to_row(memory))
            session.add(
                self._audit(
                    session,
                    memory_id=memory.memory_id,
                    op=MemoryOperation.ADD,
                    source=memory.source.value,
                    created_at=now,
                    source_message_id=proposal.source_message_id,
                )
            )
            session.delete(row)
            session.commit()
        _log.info(
            "Memory CONFIRM %s -> %s %s%s",
            proposal_id,
            memory.memory_id,
            self._describe(
                memory.scope,
                memory.type.value,
                memory.key,
                memory.thread_id,
                memory.project_id,
            ),
            f" superseded={','.join(superseded)}" if superseded else "",
        )
        return memory

    def dismiss_proposal(self, *, owner_user_id: str, proposal_id: str) -> bool:
        with Session(self._engine) as session:
            row = session.exec(
                select(MemoryProposalRow).where(
                    MemoryProposalRow.proposal_id == proposal_id,
                    MemoryProposalRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return False
            session.exec(
                delete(MemoryProposalRow).where(
                    col(MemoryProposalRow.proposal_id) == proposal_id
                )
            )
            session.commit()
        return True

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int:
        """Move memories, proposals, settings and usage totals to the new id.

        Returns the number of memory rows moved. Settings keep the verified
        account's own row when one exists (a returning user's preferences
        beat an anonymous session's); usage totals always add up, since both
        columns count real spending.
        """
        with Session(self._engine) as session:
            memories = session.exec(
                select(MemoryRow).where(
                    MemoryRow.owner_user_id == anonymous_user_id
                )
            ).all()
            for row in memories:
                row.owner_user_id = verified_user_id
                session.add(row)

            proposals = session.exec(
                select(MemoryProposalRow).where(
                    MemoryProposalRow.owner_user_id == anonymous_user_id
                )
            ).all()
            for proposal in proposals:
                proposal.owner_user_id = verified_user_id
                session.add(proposal)

            anon_settings = session.get(UserSettingsRow, anonymous_user_id)
            if anon_settings is not None:
                if session.get(UserSettingsRow, verified_user_id) is None:
                    session.add(
                        UserSettingsRow(
                            owner_user_id=verified_user_id,
                            memory_enabled=anon_settings.memory_enabled,
                            display_name=anon_settings.display_name,
                            email=anon_settings.email,
                            created_at=anon_settings.created_at,
                            updated_at=datetime.now(UTC),
                        )
                    )
                session.delete(anon_settings)

            anon_usage = session.get(LlmUsageRow, anonymous_user_id)
            if anon_usage is not None:
                verified_usage = session.get(LlmUsageRow, verified_user_id)
                if verified_usage is None:
                    session.add(
                        LlmUsageRow(
                            owner_user_id=verified_user_id,
                            prompt_tokens=anon_usage.prompt_tokens,
                            completion_tokens=anon_usage.completion_tokens,
                            total_tokens=anon_usage.total_tokens,
                            requests=anon_usage.requests,
                            updated_at=datetime.now(UTC),
                        )
                    )
                else:
                    verified_usage.prompt_tokens += anon_usage.prompt_tokens
                    verified_usage.completion_tokens += anon_usage.completion_tokens
                    verified_usage.total_tokens += anon_usage.total_tokens
                    verified_usage.requests += anon_usage.requests
                    verified_usage.updated_at = datetime.now(UTC)
                    session.add(verified_usage)
                session.delete(anon_usage)

            session.commit()
            return len(memories)

    # --- settings -----------------------------------------------------------

    def get_settings(self, *, owner_user_id: str) -> UserSettings:
        with Session(self._engine) as session:
            row = session.get(UserSettingsRow, owner_user_id)
            if row is None:
                return self._default_settings(owner_user_id)
            return UserSettings(
                owner_user_id=row.owner_user_id,
                memory_enabled=row.memory_enabled,
                display_name=row.display_name,
                email=row.email,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def set_memory_enabled(
        self, *, owner_user_id: str, enabled: bool
    ) -> UserSettings:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.get(UserSettingsRow, owner_user_id)
            if row is None:
                row = UserSettingsRow(
                    owner_user_id=owner_user_id,
                    memory_enabled=enabled,
                    created_at=now,
                    updated_at=now,
                )
            else:
                row.memory_enabled = enabled
                row.updated_at = now
            session.add(row)
            session.commit()
            session.refresh(row)
            return UserSettings(
                owner_user_id=row.owner_user_id,
                memory_enabled=row.memory_enabled,
                display_name=row.display_name,
                email=row.email,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def update_profile(
        self,
        *,
        owner_user_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> UserSettings:
        """Store the login provider's profile. None leaves a field unchanged."""
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.get(UserSettingsRow, owner_user_id)
            if row is None:
                row = UserSettingsRow(
                    owner_user_id=owner_user_id,
                    created_at=now,
                    updated_at=now,
                )
            if display_name is not None:
                row.display_name = display_name.strip() or None
            if email is not None:
                row.email = email.strip() or None
            row.updated_at = now
            session.add(row)
            session.commit()
            session.refresh(row)
            return UserSettings(
                owner_user_id=row.owner_user_id,
                memory_enabled=row.memory_enabled,
                display_name=row.display_name,
                email=row.email,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    # --- llm usage ----------------------------------------------------------

    def add_llm_usage(
        self, *, owner_user_id: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.get(LlmUsageRow, owner_user_id)
            if row is None:
                row = LlmUsageRow(owner_user_id=owner_user_id, updated_at=now)
            row.prompt_tokens += max(0, prompt_tokens)
            row.completion_tokens += max(0, completion_tokens)
            row.total_tokens = row.prompt_tokens + row.completion_tokens
            row.requests += 1
            row.updated_at = now
            session.add(row)
            session.commit()

    def get_llm_usage(self, *, owner_user_id: str) -> LlmUsage:
        with Session(self._engine) as session:
            row = session.get(LlmUsageRow, owner_user_id)
            if row is None:
                return LlmUsage(owner_user_id=owner_user_id)
            return LlmUsage(
                owner_user_id=row.owner_user_id,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                requests=row.requests,
            )

    def close(self) -> None:
        self._engine.dispose()

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _default_settings(owner_user_id: str) -> UserSettings:
        now = datetime.now(UTC)
        return UserSettings(
            owner_user_id=owner_user_id,
            memory_enabled=True,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _describe(
        scope: MemoryScope,
        type_: str,
        key: str,
        thread_id: str | None,
        project_id: str | None,
    ) -> str:
        """Identify a memory in a log line without quoting its value.

        The value is the user's own personal data and the one field that must
        not reach a log file, so the scope, type and key carry the meaning and
        the memory id is what leads back to the row itself.
        """
        target = thread_id or project_id
        scoped = f"{scope.value}:{target}" if target else scope.value
        return f"{scoped}/{type_}/{key}"

    @staticmethod
    def _scope_lock_key(memory: Memory) -> int:
        """A stable 64-bit lock id for one owner's one scope target.

        Hashed rather than taken from a row, because the user scope has no row
        to lock: `user_settings` is created lazily and a user who has never
        touched a setting has none. Blake2b rather than `hash()`, which is salted
        per process, and rather than Postgres `hashtext`, whose result is not
        promised to be stable across versions.
        """
        target = ""
        if memory.scope is MemoryScope.THREAD:
            target = memory.thread_id or ""
        elif memory.scope is MemoryScope.PROJECT:
            target = memory.project_id or ""
        identity = f"{memory.owner_user_id}\x00{memory.scope.value}\x00{target}"
        digest = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=True)

    def _claim_slot(
        self,
        session: Session,
        memory: Memory,
        *,
        now: datetime,
        exclude: str | None = None,
    ) -> list[str]:
        """Take the scope's slot for `memory.key`, or refuse.

        Cap check and supersede in one place, behind a lock, because they are one
        decision: read the live rows, decide, write. Nothing serialized those
        three steps before, so two writers could both see room under the cap and
        both take the last slot, or both insert on one key and neither supersede
        the other. The lock is transaction-scoped, so it is released by the commit
        or the rollback that follows and never needs unlocking by hand.

        `exclude` is the row already living in this scope that is doing the
        claiming, which is how a promotion avoids superseding or counting itself.

        Returns the ids it superseded. Raises `ValueError` when the cap blocks
        the write, which is the caller's cue to propose instead.
        """
        session.exec(select(func.pg_advisory_xact_lock(self._scope_lock_key(memory))))
        live = [
            row
            for row in self._live_in_scope(session, memory)
            if row.memory_id != exclude
        ]
        cap = self._caps[memory.scope]
        if len(live) >= cap and not any(row.key == memory.key for row in live):
            evicted = self._evict_least_recently_used(session, live, now=now)
            if evicted is None:
                raise ValueError(
                    f"Memory cap reached for scope {memory.scope.value!r} "
                    f"({cap}); delete something first"
                )
            live = [row for row in live if row.memory_id != evicted]
        superseded: list[str] = []
        for row in live:
            if row.key != memory.key:
                continue
            row.superseded_by = memory.memory_id
            # Also marked deleted: the read path filters on deleted_at alone, so
            # a superseded row would otherwise still load.
            row.deleted_at = now
            row.updated_at = now
            session.add(row)
            superseded.append(row.memory_id)
        # Flushed before the caller inserts its own row: the partial unique index
        # on the live key would otherwise see the old row and the new one at once
        # and reject a supersede that is perfectly correct.
        if superseded:
            session.flush()
        return superseded

    def _evict_least_recently_used(
        self, session: Session, live: list[MemoryRow], *, now: datetime
    ) -> str | None:
        """Make room by dropping the row that has gone longest without use.

        The cap is a budget, not a wall: an automatic writer that cannot write
        forgets whatever the user just said, silently. So a full scope gives
        one row up instead of refusing.

        Only rows this pipeline wrote itself are eligible. What the user typed
        is not ours to throw away, and a scope full of user-written memory
        still raises, which is the caller's cue to propose.

        Rows are ranked by when they last mattered -- read into a prompt, or
        failing that, saved -- which is the same rule that decides which
        memories a prompt carries. Treating "never used" as its own worst
        bucket threw away a memory saved seconds ago ahead of one last read a
        year earlier.
        """
        evictable = [row for row in live if row.source == MemorySource.LLM_EXTRACTED.value]
        if not evictable:
            return None
        target = min(
            evictable,
            key=lambda row: (row.last_used_at or row.created_at, row.created_at),
        )
        target.deleted_at = now
        target.updated_at = now
        session.add(target)
        session.add(
            self._audit(
                session,
                memory_id=target.memory_id,
                op=MemoryOperation.DELETE,
                source=target.source,
                created_at=now,
            )
        )
        session.flush()
        _log.info(
            "Memory EVICT %s %s last_used=%s (scope full)",
            target.memory_id,
            self._describe(
                MemoryScope(target.scope),
                target.type,
                target.key,
                target.thread_id,
                target.project_id,
            ),
            target.last_used_at.isoformat() if target.last_used_at else "never",
        )
        return target.memory_id

    @staticmethod
    def _live_in_scope(session: Session, memory: Memory) -> list[MemoryRow]:
        statement = select(MemoryRow).where(
            MemoryRow.owner_user_id == memory.owner_user_id,
            MemoryRow.scope == memory.scope.value,
            col(MemoryRow.deleted_at).is_(None),
        )
        if memory.scope is MemoryScope.THREAD:
            statement = statement.where(MemoryRow.thread_id == memory.thread_id)
        elif memory.scope is MemoryScope.PROJECT:
            statement = statement.where(MemoryRow.project_id == memory.project_id)
        return list(session.exec(statement).all())

    @staticmethod
    def _owned(
        session: Session, owner_user_id: str, memory_id: str, include_deleted: bool
    ) -> MemoryRow | None:
        statement = select(MemoryRow).where(
            MemoryRow.memory_id == memory_id,
            MemoryRow.owner_user_id == owner_user_id,
        )
        if not include_deleted:
            statement = statement.where(col(MemoryRow.deleted_at).is_(None))
        return session.exec(statement).first()

    @staticmethod
    def _to_row(memory: Memory) -> MemoryRow:
        return MemoryRow(
            memory_id=memory.memory_id,
            owner_user_id=memory.owner_user_id,
            scope=memory.scope.value,
            thread_id=memory.thread_id,
            project_id=memory.project_id,
            type=memory.type.value,
            key=memory.key,
            value=memory.value,
            source=memory.source.value,
            superseded_by=memory.superseded_by,
            last_used_at=memory.last_used_at,
            deleted_at=memory.deleted_at,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )

    @staticmethod
    def _audit(
        session: Session,
        *,
        memory_id: str,
        op: MemoryOperation,
        source: str,
        created_at: datetime,
        source_message_id: str | None = None,
    ) -> MemoryAuditRow:
        """Build an audit row with the next per-memory sequence.

        The trail is ordered by `sequence`, not `created_at`: the clock
        resolution on some hosts puts an ADD and the UPDATE that follows it on
        one identical timestamp, which would leave their order arbitrary.
        """
        current = session.exec(
            select(func.max(col(MemoryAuditRow.sequence))).where(
                col(MemoryAuditRow.memory_id) == memory_id
            )
        ).one()
        return MemoryAuditRow(
            audit_id=f"aud_{uuid.uuid4().hex}",
            memory_id=memory_id,
            op=op.value,
            source=source,
            source_message_id=source_message_id,
            created_at=created_at,
            sequence=int(current or 0) + 1,
        )

    @staticmethod
    def _to_memory(row: MemoryRow) -> Memory:
        return Memory(
            memory_id=row.memory_id,
            owner_user_id=row.owner_user_id,
            scope=MemoryScope(row.scope),
            thread_id=row.thread_id,
            project_id=row.project_id,
            type=MemoryType(row.type),
            key=row.key,
            value=row.value,
            source=MemorySource(row.source),
            superseded_by=row.superseded_by,
            last_used_at=row.last_used_at,
            deleted_at=row.deleted_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_proposal(row: MemoryProposalRow) -> MemoryProposal:
        return MemoryProposal(
            proposal_id=row.proposal_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            suggested_scope=MemoryScope(row.suggested_scope),
            type=MemoryType(row.type),
            key=row.key,
            value=row.value,
            source_message_id=row.source_message_id,
            created_at=row.created_at,
        )
