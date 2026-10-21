"""Request/response models for every route, in one place.

Routers import from here; nothing here imports a router, so the models stay
usable from tests and scripts without dragging FastAPI wiring along.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from rag_ingestion.chunking import Chunk
from rag_ingestion.models import StrictModel
from rag_ingestion.providers.registry import Provider
from rag_ingestion.memory.models import (
    MemoryScope,
    MemoryType,
    is_valid_key,
)
from rag_ingestion.storage.projects import FileProcessingStatus, Project, ProjectRole
from rag_ingestion.storage.threads import (
    ShareAccess,
    Thread,
    ThreadMessage,
    ThreadMessageRole,
)


class CreateProjectRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    provider: Provider
    #: Display icon key from the frontend's icon set; free-form so the set can
    #: grow without an API change.
    icon: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be blank")
        return normalized


#: The one email shape both invitations accept. Deliberately loose, and
#: `email-validator` deliberately not a dependency: the address is only ever
#: compared against one a login provider already verified, so a wrong one
#: simply never matches. This catches the typo, nothing more.
_EMAIL_PATTERN = r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$"


class ProjectMemberRequest(StrictModel):
    """Add somebody, or change the role they already have."""

    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)
    #: OWNER is not offered: ownership moves through the transfer route, which
    #: also demotes the previous owner, and accepting it here would leave a
    #: project with two of them.
    role: Literal["viewer", "editor"] = "viewer"


class RemoveProjectMemberRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)


class TransferProjectRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)


class ProjectMemberView(StrictModel):
    email: str
    role: ProjectRole


class ProjectView(Project):
    """A project as the API returns it, plus what the caller may do with it.

    The role rides along so one `GET /projects` serves both the sidebar and
    the "Shared with you" split, and so the UI can hide what would 403.
    """

    role: ProjectRole
    #: People besides the owner. Non-zero means the project is shared, which is
    #: what stops the UI calling a shared project "Private".
    member_count: int = 0


class RenameProjectRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be blank")
        return normalized


class QueryRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4_000)
    language: str | None = Field(default=None, min_length=2, max_length=16)
    limit: int = Field(default=10, ge=1, le=100)
    #: Documents the caller unchecked for this question. Additive to the
    #: server-side trash exclusion; unknown ids are harmless (nothing matches).
    disabled_doc_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Query must not be blank")
        return normalized


class QueryHit(StrictModel):
    chunk_id: str
    doc_id: str
    score: float
    level: str
    source_text: str
    source_block_ids: list[str]
    source_span: list[int] | None = None
    locators: list[dict[str, object]]
    media_refs: list[str]
    confidence: float
    citable: bool
    parent_id: str | None = None
    section_path: list[str]
    refs: list[str]
    breadcrumb: str

    @classmethod
    def from_chunk(cls, chunk: Chunk, score: float) -> QueryHit:
        return cls(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            score=score,
            level=chunk.level.value,
            source_text=chunk.source_text,
            source_block_ids=chunk.source_block_ids,
            source_span=list(chunk.source_span) if chunk.source_span else None,
            locators=[locator.model_dump(mode="json") for locator in chunk.locators],
            media_refs=chunk.media_refs,
            confidence=chunk.confidence,
            citable=chunk.citable,
            parent_id=chunk.parent_id,
            section_path=chunk.section_path,
            refs=chunk.refs,
            breadcrumb=chunk.index_text.split("\n", 1)[0],
        )


class QueryResponse(StrictModel):
    query: str
    hits: list[QueryHit]
    evidence_leaf_ids: list[str] = []
    routed_doc_ids: list[str] = []
    diagnostics: dict[str, int] = {}


class AnswerResponse(QueryResponse):
    answer: str
    citations: list[str]


class CreateThreadRequest(StrictModel):
    title: str = Field(default="New chat", min_length=1, max_length=200)
    project_id: str | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Thread title must not be blank")
        return normalized


class RenameThreadRequest(StrictModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Thread title must not be blank")
        return normalized


class ThreadMessageRequest(StrictModel):
    content: str = Field(min_length=1, max_length=4_000)
    retrieval_limit: int = Field(default=10, ge=1, le=100)
    disabled_doc_ids: list[str] = Field(default_factory=list, max_length=200)
    #: The picker's choice for this turn. None keeps the server's configured
    #: model; an id the catalog no longer carries falls back to the default.
    model_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message content must not be blank")
        return normalized


class ThreadView(Thread):
    """A thread as the API returns it: storage fields plus the computed gauge.

    The percentage is computed server-side (`model_catalog.context_percent`)
    so the frontend renders a number instead of re-deriving budgets from raw
    token counts. None until the thread's first measured turn.
    """

    context_used_percent: int | None = None


class ThreadDetail(ThreadView):
    messages: list[ThreadMessage]


class ForkThreadRequest(StrictModel):
    """Where the branch is cut. A position past the end copies what is there,
    rather than 422-ing on a race with a turn that arrived meanwhile."""

    through_position: int = Field(ge=1)


#: The closed list the report dialog's dropdown offers.
ReportReason = Literal["harmful", "false_information", "not_helpful", "other"]


class CreateReportRequest(StrictModel):
    thread_id: str = Field(min_length=1, max_length=64)
    #: Which message. Omitted means the report is about the chat as a whole.
    message_position: int | None = Field(default=None, ge=1)
    reason: ReportReason
    #: Matches the dialog's 1000-character counter.
    details: str | None = Field(default=None, max_length=1000)


class ReportView(StrictModel):
    report_id: str
    created_at: datetime


class CreateShareRequest(StrictModel):
    """`refresh` extends an existing link's snapshot to the thread as it is
    now. Opt-in rather than automatic: reopening the dialog to copy the link
    again must never quietly publish turns written since.

    `access` switches an existing share between the public link and the
    invite list; omitted leaves it where it is.
    """

    refresh: bool = False
    access: ShareAccess | None = None


class ShareRecipientRequest(StrictModel):
    """One invitation.

    The pattern is deliberately loose, and `email-validator` deliberately not
    a dependency: the address is only ever compared against one a login
    provider already verified, so an invitation that is not a real address
    simply never matches anybody. This catches the typo that would otherwise
    sit in the owner's list looking like it worked; it is not, and does not
    need to be, RFC 5322.
    """

    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)


class ShareView(StrictModel):
    """The owner's view of a link. `message_count` is the thread's length now,
    so the dialog can tell that the snapshot has fallen behind."""

    share_id: str
    shared_through_position: int
    message_count: int
    access: ShareAccess
    recipients: list[str]
    created_at: datetime
    updated_at: datetime


class SharedWithMeItem(StrictModel):
    """One row of "Shared with you". Carries no ids of the owner's."""

    share_id: str
    title: str
    updated_at: datetime


class SharedMessage(StrictModel):
    """One message as a public viewer sees it: no ids of any kind."""

    role: ThreadMessageRole
    content: str
    created_at: datetime


class SharedChatView(StrictModel):
    """A shared chat, scrubbed.

    Everything identifying is left out on purpose -- owner, thread, project,
    message ids, and the citations, whose chunk and document ids describe the
    owner's private library. The `[cite:n]` markers in the text still render
    as plain chips.
    """

    title: str
    created_at: datetime
    messages: list[SharedMessage]
    #: Length of the snapshot, which can exceed the messages returned.
    message_count: int
    #: True when the cap cut the transcript, so the page can say so rather
    #: than present a partial conversation as the whole thing.
    truncated: bool
    #: Whether this reader may take the conversation into a chat of their own.
    #: Invite shares only; a public link stays the read-only snapshot.
    can_continue: bool = False


class CompactThreadRequest(StrictModel):
    """Optional steer for the fold: what this compaction should watch for
    when it decides which facts are worth keeping."""

    instructions: str | None = Field(default=None, max_length=2000)


class CompactThreadResponse(StrictModel):
    """Outcome of a click-to-compact. After a fold, `context_used_percent`
    is re-estimated from what the next prompt will actually carry (floor
    window plus summary, at the thread's measured chars/token); the next
    real turn replaces the estimate with a measurement. `stalled` is the
    difference between "nothing to compact" and "the click did not work":
    backlog was left behind, by a failed fold or the iteration ceiling."""

    changed: bool
    stalled: bool
    summarized_through_position: int | None
    context_used_percent: int | None


class ModelOption(StrictModel):
    id: str
    label: str


class ModelCatalogResponse(StrictModel):
    default: str
    models: list[ModelOption]


class CreateMemoryRequest(StrictModel):
    scope: MemoryScope
    type: MemoryType
    key: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=2_000)
    thread_id: str | None = None
    project_id: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_key(normalized):
            raise ValueError("Key must be in the closed list or match other:<slug>")
        return normalized

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Memory value must not be blank")
        return normalized


class UpdateMemoryRequest(StrictModel):
    value: str | None = Field(default=None, min_length=1, max_length=2_000)
    promote_to_user: bool = False

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Memory value must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_a_change(self) -> "UpdateMemoryRequest":
        if self.value is None and not self.promote_to_user:
            raise ValueError("Provide a value or promote_to_user")
        return self


class UpdateSettingsRequest(StrictModel):
    """Partial update: absent fields stay unchanged."""

    memory_enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class HealthResponse(StrictModel):
    status: str
    configured_providers: list[Provider]


class ProcessDocumentResponse(StrictModel):
    file_id: str
    doc_id: str
    filename: str
    source_type: str
    block_count: int
    chunk_count: int
    indexed_count: int
    status: FileProcessingStatus


class DocumentMarkdownResponse(StrictModel):
    doc_id: str
    markdown: str
    #: `block_id -> [char_start, char_end)` into `markdown`. A citation carries
    #: block ids, so this is what turns one into a highlightable range.
    anchors: dict[str, tuple[int, int]]


class UploadFileResult(StrictModel):
    index: int = Field(ge=0)
    filename: str
    #: `duplicate` = identical content already active in the project;
    #: `restored` = identical content found in the trash and flipped back
    #: without re-parsing or re-embedding.
    status: Literal["completed", "failed", "duplicate", "restored"]
    file_id: str | None = None
    doc_id: str | None = None
    source_type: str | None = None
    block_count: int | None = None
    chunk_count: int | None = None
    indexed_count: int | None = None
    detail: str | None = None

    @classmethod
    def completed(
        cls, index: int, response: ProcessDocumentResponse
    ) -> "UploadFileResult":
        return cls(index=index, **response.model_dump(mode="json"))
