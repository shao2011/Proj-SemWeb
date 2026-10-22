from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, delete, func
from sqlmodel import Session, col, select

from rag_ingestion.db.tables import ProjectFileRow, ProjectMemberRow, ProjectRow
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.storage.projects import (
    FileProcessingStatus,
    PinnedEmbeddingProfile,
    Project,
    ProjectFile,
    ProjectMember,
    ProjectRole,
)
from rag_ingestion.storage.threads import normalize_email


_ALLOWED_STATUS_TRANSITIONS = {
    FileProcessingStatus.PENDING: {FileProcessingStatus.PROCESSING},
    FileProcessingStatus.PROCESSING: {
        FileProcessingStatus.COMPLETED,
        FileProcessingStatus.FAILED,
    },
    FileProcessingStatus.COMPLETED: set(),
    FileProcessingStatus.FAILED: set(),
}


class PostgresProjectStore:
    """Project metadata in Postgres.

    Returns the same frozen Pydantic contracts as before, so routes and their
    response models are unchanged: ORM rows never escape this module.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_project(
        self,
        *,
        owner_user_id: str,
        name: str,
        embedding_profile: EmbeddingProfile,
        icon: str | None = None,
        color: str | None = None,
    ) -> Project:
        now = datetime.now(UTC)
        pinned = PinnedEmbeddingProfile.model_validate(embedding_profile.model_dump())
        project = Project(
            project_id=f"prj_{uuid.uuid4().hex}",
            owner_user_id=owner_user_id,
            name=name,
            embedding_profile=pinned,
            icon=icon,
            color=color,
            created_at=now,
            updated_at=now,
        )
        with Session(self._engine) as session:
            session.add(
                ProjectRow(
                    project_id=project.project_id,
                    owner_user_id=project.owner_user_id,
                    name=project.name,
                    visibility=project.visibility,
                    embedding_profile=json.dumps(
                        pinned.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    icon=project.icon,
                    color=project.color,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                )
            )
            session.commit()
        return project

    def get_project(self, *, owner_user_id: str, project_id: str) -> Project | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectRow).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                )
            ).first()
            return self._project(row) if row is not None else None

    def list_projects(self, *, owner_user_id: str) -> list[Project]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(ProjectRow)
                .where(ProjectRow.owner_user_id == owner_user_id)
                .order_by(ProjectRow.created_at, ProjectRow.project_id)
            ).all()
            return [self._project(row) for row in rows]

    def rename_project(
        self, *, owner_user_id: str, project_id: str, name: str
    ) -> Project | None:
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectRow).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return None
            row.name = name
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._project(row)

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int:
        """Move every project of `anonymous_user_id` to `verified_user_id`.

        Part of claiming an anonymous account after the first real login.
        Files hang off the project row, so only `projects` carries the owner.
        """
        with Session(self._engine) as session:
            rows = session.exec(
                select(ProjectRow).where(
                    ProjectRow.owner_user_id == anonymous_user_id
                )
            ).all()
            for row in rows:
                row.owner_user_id = verified_user_id
                session.add(row)
            session.commit()
            return len(rows)

    # --- membership -------------------------------------------------------------

    def get_project_by_id(self, *, project_id: str) -> Project | None:
        with Session(self._engine) as session:
            row = session.get(ProjectRow, project_id)
            return self._project(row) if row is not None else None

    def list_shared_projects(self, *, email: str) -> list[Project]:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            rows = session.exec(
                select(ProjectRow)
                .join(
                    ProjectMemberRow,
                    col(ProjectMemberRow.project_id) == col(ProjectRow.project_id),
                )
                .where(ProjectMemberRow.email == normalized)
                .order_by(col(ProjectRow.updated_at).desc())
            ).all()
            return [self._project(row) for row in rows]

    def member_role(self, *, project_id: str, email: str) -> ProjectRole | None:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == project_id,
                    ProjectMemberRow.email == normalized,
                )
            ).first()
            return ProjectRole(row.role) if row is not None else None

    def list_members(self, *, project_id: str) -> list[ProjectMember]:
        with Session(self._engine) as session:
            return self._members(session, project_id)

    def member_counts(self, *, project_ids: list[str]) -> dict[str, int]:
        if not project_ids:
            return {}
        with Session(self._engine) as session:
            rows = session.exec(
                select(
                    ProjectMemberRow.project_id,
                    func.count(col(ProjectMemberRow.member_id)),
                )
                .where(col(ProjectMemberRow.project_id).in_(project_ids))
                .group_by(col(ProjectMemberRow.project_id))
            ).all()
            return {project_id: int(total) for project_id, total in rows}

    def add_member(
        self, *, project_id: str, email: str, role: ProjectRole
    ) -> list[ProjectMember]:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            existing = session.exec(
                select(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == project_id,
                    ProjectMemberRow.email == normalized,
                )
            ).first()
            if existing is None:
                session.add(
                    ProjectMemberRow(
                        member_id=f"prm_{uuid.uuid4().hex}",
                        project_id=project_id,
                        email=normalized,
                        role=role.value,
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                # Re-inviting is how a role is changed; a second row for the
                # same address would make "their role" ambiguous.
                existing.role = role.value
                session.add(existing)
            session.commit()
            return self._members(session, project_id)

    def remove_member(self, *, project_id: str, email: str) -> bool:
        normalized = normalize_email(email)
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectMemberRow).where(
                    ProjectMemberRow.project_id == project_id,
                    ProjectMemberRow.email == normalized,
                )
            ).first()
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def transfer_owner(
        self,
        *,
        project_id: str,
        current_owner_user_id: str,
        current_owner_email: str,
        new_owner_user_id: str,
        new_owner_email: str,
    ) -> Project | None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectRow).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == current_owner_user_id,
                )
            ).first()
            if row is None:
                return None
            row.owner_user_id = new_owner_user_id
            row.updated_at = now
            session.add(row)
            # One transaction for both sides of the swap: a crash between them
            # would leave a project whose owner is also listed as a member, or
            # whose previous owner has lost it entirely.
            session.exec(
                delete(ProjectMemberRow).where(
                    col(ProjectMemberRow.project_id) == project_id,
                    col(ProjectMemberRow.email).in_(
                        [
                            normalize_email(new_owner_email),
                            normalize_email(current_owner_email),
                        ]
                    ),
                )
            )
            session.add(
                ProjectMemberRow(
                    member_id=f"prm_{uuid.uuid4().hex}",
                    project_id=project_id,
                    email=normalize_email(current_owner_email),
                    role=ProjectRole.EDITOR.value,
                    created_at=now,
                )
            )
            session.commit()
            session.refresh(row)
            return self._project(row)

    @staticmethod
    def _members(session: Session, project_id: str) -> list[ProjectMember]:
        rows = session.exec(
            select(ProjectMemberRow)
            .where(ProjectMemberRow.project_id == project_id)
            .order_by(col(ProjectMemberRow.created_at))
        ).all()
        return [
            ProjectMember(email=row.email, role=ProjectRole(row.role)) for row in rows
        ]

    def delete_project(self, *, owner_user_id: str, project_id: str) -> bool:
        with Session(self._engine) as session:
            row = session.exec(
                select(ProjectRow).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                )
            ).first()
            if row is None:
                return False
            # Explicit child delete: the ORM does not emit the database-level
            # ON DELETE CASCADE unless the parent's children are loaded.
            session.exec(
                delete(ProjectFileRow).where(
                    col(ProjectFileRow.project_id) == project_id
                )
            )
            session.delete(row)
            session.commit()
            return True

    def add_file(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        filename: str,
        content_hash: str,
    ) -> ProjectFile | None:
        now = datetime.now(UTC)
        with Session(self._engine) as session:
            owns = session.exec(
                select(ProjectRow.project_id).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                )
            ).first()
            if owns is None:
                return None
            record = ProjectFile(
                file_id=f"file_{uuid.uuid4().hex}",
                project_id=project_id,
                owner_user_id=owner_user_id,
                filename=filename,
                content_hash=content_hash,
                status=FileProcessingStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            session.add(
                ProjectFileRow(
                    file_id=record.file_id,
                    project_id=record.project_id,
                    filename=record.filename,
                    content_hash=record.content_hash,
                    doc_id=record.doc_id,
                    status=record.status.value,
                    error=record.error,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            session.commit()
        return record

    def get_file(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        file_id: str,
    ) -> ProjectFile | None:
        with Session(self._engine) as session:
            row = self._owned_file(session, owner_user_id, project_id, file_id)
            return self._file(row, owner_user_id) if row is not None else None

    def list_files(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        include_trashed: bool = False,
    ) -> list[ProjectFile] | None:
        with Session(self._engine) as session:
            owns = session.exec(
                select(ProjectRow.project_id).where(
                    ProjectRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                )
            ).first()
            if owns is None:
                return None
            statement = (
                select(ProjectFileRow)
                .where(ProjectFileRow.project_id == project_id)
                .order_by(ProjectFileRow.created_at, ProjectFileRow.file_id)
            )
            if not include_trashed:
                statement = statement.where(col(ProjectFileRow.trashed_at).is_(None))
            rows = session.exec(statement).all()
            return [self._file(row, owner_user_id) for row in rows]

    def find_files_by_hash(
        self, *, owner_user_id: str, project_id: str, content_hash: str
    ) -> list[ProjectFile]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(ProjectFileRow)
                .join(ProjectRow, ProjectRow.project_id == ProjectFileRow.project_id)
                .where(
                    ProjectFileRow.project_id == project_id,
                    ProjectRow.owner_user_id == owner_user_id,
                    ProjectFileRow.content_hash == content_hash,
                )
                .order_by(ProjectFileRow.created_at, ProjectFileRow.file_id)
            ).all()
            return [self._file(row, owner_user_id) for row in rows]

    def trash_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None:
        with Session(self._engine) as session:
            row = self._owned_file(session, owner_user_id, project_id, file_id)
            if row is None:
                return None
            now = datetime.now(UTC)
            row.trashed_at = now
            row.updated_at = now
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._file(row, owner_user_id)

    def restore_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None:
        with Session(self._engine) as session:
            row = self._owned_file(session, owner_user_id, project_id, file_id)
            if row is None:
                return None
            row.trashed_at = None
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._file(row, owner_user_id)

    def delete_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None:
        with Session(self._engine) as session:
            row = self._owned_file(session, owner_user_id, project_id, file_id)
            if row is None:
                return None
            record = self._file(row, owner_user_id)
            session.delete(row)
            session.commit()
            return record

    def update_file_status(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        file_id: str,
        status: FileProcessingStatus,
        error: str | None = None,
        doc_id: str | None = None,
    ) -> ProjectFile | None:
        if status is FileProcessingStatus.FAILED and not error:
            raise ValueError("Failed files require an error")
        if status is not FileProcessingStatus.FAILED and error is not None:
            raise ValueError("Only failed files may have an error")
        if status is FileProcessingStatus.COMPLETED and not doc_id:
            raise ValueError("Completed files require a document ID")
        if status is not FileProcessingStatus.COMPLETED and doc_id is not None:
            raise ValueError("Only completed files may have a document ID")

        with Session(self._engine) as session:
            row = self._owned_file(session, owner_user_id, project_id, file_id)
            if row is None:
                return None
            current = FileProcessingStatus(row.status)
            if status not in _ALLOWED_STATUS_TRANSITIONS[current]:
                raise ValueError(
                    f"Invalid file status transition: {current.value} -> {status.value}"
                )
            row.status = status.value
            row.error = error
            row.doc_id = doc_id
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._file(row, owner_user_id)

    def close(self) -> None:
        self._engine.dispose()

    @staticmethod
    def _owned_file(
        session: Session, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFileRow | None:
        return session.exec(
            select(ProjectFileRow)
            .join(ProjectRow, ProjectRow.project_id == ProjectFileRow.project_id)
            .where(
                ProjectFileRow.file_id == file_id,
                ProjectFileRow.project_id == project_id,
                ProjectRow.owner_user_id == owner_user_id,
            )
        ).first()

    @staticmethod
    def _project(row: ProjectRow) -> Project:
        return Project(
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            name=row.name,
            visibility=row.visibility,
            embedding_profile=PinnedEmbeddingProfile.model_validate_json(
                row.embedding_profile
            ),
            icon=row.icon,
            color=row.color,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _file(row: ProjectFileRow, owner_user_id: str) -> ProjectFile:
        return ProjectFile(
            file_id=row.file_id,
            project_id=row.project_id,
            owner_user_id=owner_user_id,
            filename=row.filename,
            content_hash=row.content_hash,
            doc_id=row.doc_id,
            status=FileProcessingStatus(row.status),
            error=row.error,
            trashed_at=row.trashed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
