from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from rag_ingestion.models import StrictModel
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile
from rag_ingestion.storage.threads import normalize_email

__all__ = ["normalize_email"]  # re-exported: one spelling of address handling


class PinnedEmbeddingProfile(EmbeddingProfile):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Project(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    visibility: Literal["private"] = "private"
    embedding_profile: PinnedEmbeddingProfile
    #: Display icon key and hex color chosen by the user; None = defaults.
    icon: str | None = None
    color: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectRole(StrEnum):
    """What someone may do in a project, weakest first.

    Ordered so a route can say "editor or better" without a lookup table; see
    `at_least`. OWNER is never stored in `project_members` -- it is derived
    from `ProjectRow.owner_user_id`, so the two can never disagree.
    """

    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"

    def at_least(self, needed: ProjectRole) -> bool:
        order = [ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.OWNER]
        return order.index(self) >= order.index(needed)


class ProjectMember(StrictModel):
    """One person on a project. Deliberately no uid: the owner invites an
    address, and the person behind it may not have signed up yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    email: str = Field(min_length=3, max_length=320)
    role: ProjectRole


class FileProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectFile(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    doc_id: str | None = None
    status: FileProcessingStatus
    error: str | None = None
    #: None = active. Set = in the trash, hidden from retrieval but with
    #: vectors and media intact for TTL-bounded free restore.
    trashed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_processing_error(self) -> ProjectFile:
        if self.status is FileProcessingStatus.FAILED and not self.error:
            raise ValueError("Failed files require an error")
        if self.status is not FileProcessingStatus.FAILED and self.error is not None:
            raise ValueError("Only failed files may have an error")
        return self


@runtime_checkable
class ProjectStore(Protocol):
    def create_project(
        self,
        *,
        owner_user_id: str,
        name: str,
        embedding_profile: EmbeddingProfile,
        icon: str | None = None,
        color: str | None = None,
    ) -> Project: ...

    def get_project(
        self, *, owner_user_id: str, project_id: str
    ) -> Project | None: ...

    def list_projects(self, *, owner_user_id: str) -> list[Project]: ...

    def rename_project(
        self, *, owner_user_id: str, project_id: str, name: str
    ) -> Project | None: ...

    def delete_project(self, *, owner_user_id: str, project_id: str) -> bool: ...

    def reassign_owner(
        self, *, anonymous_user_id: str, verified_user_id: str
    ) -> int: ...

    # --- membership -----------------------------------------------------------

    def get_project_by_id(self, *, project_id: str) -> Project | None:
        """The project regardless of who owns it.

        Owner-free on purpose: resolving whether a *member* may act needs the
        project first, and every caller pairs this with a role check.
        """
        ...

    def list_shared_projects(self, *, email: str) -> list[Project]:
        """Projects this address was added to, owner's projects excluded."""
        ...

    def member_role(self, *, project_id: str, email: str) -> ProjectRole | None:
        """The stored role for an address, or None when they are not a member.
        Never returns OWNER; ownership is not stored here."""
        ...

    def list_members(self, *, project_id: str) -> list[ProjectMember]: ...

    def member_counts(self, *, project_ids: list[str]) -> dict[str, int]:
        """How many members each project has, in one query.

        Feeds the label that tells an owner their project is shared. Batched
        because it is needed for every row of the project list.
        """
        ...

    def add_member(
        self, *, project_id: str, email: str, role: ProjectRole
    ) -> list[ProjectMember]:
        """Add or re-role one address. Returns the list as it now stands."""
        ...

    def remove_member(self, *, project_id: str, email: str) -> bool: ...

    def transfer_owner(
        self,
        *,
        project_id: str,
        current_owner_user_id: str,
        current_owner_email: str,
        new_owner_user_id: str,
        new_owner_email: str,
    ) -> Project | None:
        """Hand the project over, in one transaction.

        The new owner stops being a member (ownership is not stored there) and
        the previous owner becomes an editor: handing a project over should not
        lock the person who built it out of it.
        """
        ...

    def add_file(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        filename: str,
        content_hash: str,
    ) -> ProjectFile | None: ...

    def get_file(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        file_id: str,
    ) -> ProjectFile | None: ...

    def list_files(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        include_trashed: bool = False,
    ) -> list[ProjectFile] | None: ...

    def find_files_by_hash(
        self, *, owner_user_id: str, project_id: str, content_hash: str
    ) -> list[ProjectFile]: ...

    def trash_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None: ...

    def restore_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None: ...

    def delete_file(
        self, *, owner_user_id: str, project_id: str, file_id: str
    ) -> ProjectFile | None: ...

    def update_file_status(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        file_id: str,
        status: FileProcessingStatus,
        error: str | None = None,
        doc_id: str | None = None,
    ) -> ProjectFile | None: ...

    def close(self) -> None: ...


_ALLOWED_STATUS_TRANSITIONS = {
    FileProcessingStatus.PENDING: {FileProcessingStatus.PROCESSING},
    FileProcessingStatus.PROCESSING: {
        FileProcessingStatus.COMPLETED,
        FileProcessingStatus.FAILED,
    },
    FileProcessingStatus.COMPLETED: set(),
    FileProcessingStatus.FAILED: set(),
}
