"""Project routes: CRUD, documents, media, uploads, and retrieval.

The upload worker functions at the top run on the upload executor's threads,
so they take everything they need as arguments instead of reading a request
context that will not exist there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import Future, as_completed, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import FastAPI, File, Header, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from rag_ingestion.chunking import chunk_document
from rag_ingestion.config import ChunkingConfig
from rag_ingestion.logger import get_logger
from rag_ingestion.ingestion.markdown import markdown_keys
from rag_ingestion.ingestion.markdown import store as markdown_store
from rag_ingestion.ingestion.parsers.datalab import content_type_for
from rag_ingestion.ingestion.parsers.datalab import DocumentConverter
from rag_ingestion.ingestion.parsers.pdf import ScanParser
from rag_ingestion.ingestion.pipeline import process_document
from rag_ingestion.ingestion.router import route_file
from rag_ingestion.storage.media import MediaStore
from rag_ingestion.storage.projects import (
    FileProcessingStatus,
    Project,
    ProjectFile,
    ProjectRole,
    ProjectStore,
    normalize_email,
)
from rag_ingestion.providers.registry import ProviderBundle, ProviderError
from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
from rag_ingestion.api.deps import (
    ApiContext,
    context_evidence,
    sse,
    to_response,
    user_id,
)
from rag_ingestion.api.auth import authenticated_email, is_anonymous_id
from rag_ingestion.api.schemas import (
    AnswerResponse,
    CreateProjectRequest,
    DocumentMarkdownResponse,
    ProcessDocumentResponse,
    ProjectMemberRequest,
    ProjectMemberView,
    ProjectView,
    QueryRequest,
    QueryResponse,
    RemoveProjectMemberRequest,
    RenameProjectRequest,
    TransferProjectRequest,
    UploadFileResult,
)

#: Streaming buffer for an upload write. An implementation detail with no
#: operational reason to differ per deployment, so it stays out of config.
_UPLOAD_CHUNK_SIZE = 1024 * 1024

_log = get_logger(__name__)


def _upload_size(file: UploadFile) -> int:
    if file.size is not None:
        return file.size
    position = file.file.tell()
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(position)
    return size


def _upload_completed(future: Future) -> bool:
    """Whether a finished upload future changed what retrieval can see."""
    if future.cancelled() or future.exception() is not None:
        return False
    # A restore flips a trashed document back into the retrievable set, so it
    # invalidates cached outcomes exactly like a fresh index does.
    return future.result().status in ("completed", "restored")


def _purge_file(
    *,
    record: ProjectFile,
    owner_user_id: str,
    project_id: str,
    projects: ProjectStore,
    hybrid_index: QdrantHybridIndex,
    media_store: MediaStore | None,
) -> None:
    """Hard-delete one file record, plus its vectors and media when it holds
    the last reference to its document.

    The last-reference guard exists for rows created before content-hash
    dedup: two of them can point at the same ``doc_id``, and purging one must
    not take the survivor's evidence with it.
    """
    if record.doc_id:
        siblings = [
            other
            for other in (
                projects.list_files(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    include_trashed=True,
                )
                or []
            )
            if other.doc_id == record.doc_id and other.file_id != record.file_id
        ]
        if not siblings:
            hybrid_index.delete_by_doc(
                owner_user_id=owner_user_id,
                project_id=project_id,
                doc_id=record.doc_id,
            )
            if media_store is not None:
                try:
                    media_store.delete_prefix(record.doc_id)
                except OSError as error:
                    _log.warning(
                        "Media cleanup failed for doc=%s in project=%s: %s",
                        record.doc_id,
                        project_id,
                        error,
                    )
    projects.delete_file(
        owner_user_id=owner_user_id, project_id=project_id, file_id=record.file_id
    )


def _process_one_upload(
    *,
    position: int,
    file: UploadFile,
    owner_user_id: str,
    project_id: str,
    projects: ProjectStore,
    hybrid_index: QdrantHybridIndex,
    bundle: ProviderBundle,
    max_upload_bytes: int,
    chunking: ChunkingConfig,
    scan_parser: ScanParser | None,
    converter: DocumentConverter | None,
    media_store: MediaStore | None,
    trash_ttl_seconds: int,
) -> UploadFileResult:
    filename = Path(file.filename or "").name
    record: ProjectFile | None = None

    def failed(detail: str, stored_error: str | None = None) -> UploadFileResult:
        if record is not None and stored_error is not None:
            try:
                projects.update_file_status(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    file_id=record.file_id,
                    status=FileProcessingStatus.FAILED,
                    error=stored_error,
                )
            except Exception:
                _log.exception(
                    "Could not mark upload failed for project=%s file=%s",
                    project_id,
                    filename,
                )
        return UploadFileResult(
            index=position,
            filename=filename,
            status="failed",
            file_id=record.file_id if record is not None else None,
            detail=detail,
        )

    if not filename:
        return failed("Upload filename is required")
    try:
        route_file(Path(filename))
    except ValueError as error:
        return failed(str(error))

    try:
        with TemporaryDirectory(prefix="rag-upload-") as directory:
            upload_path = Path(directory) / filename
            digest = hashlib.sha256()
            size = 0
            file.file.seek(0)
            with upload_path.open("wb") as destination:
                while data := file.file.read(_UPLOAD_CHUNK_SIZE):
                    size += len(data)
                    if size > max_upload_bytes:
                        return failed(
                            f"Upload exceeds the {max_upload_bytes}-byte limit"
                        )
                    digest.update(data)
                    destination.write(data)
            if size == 0:
                return failed("Uploaded file is empty")

            content_hash = digest.hexdigest()
            matches = projects.find_files_by_hash(
                owner_user_id=owner_user_id,
                project_id=project_id,
                content_hash=content_hash,
            )
            active_match = next(
                (
                    match
                    for match in matches
                    if match.trashed_at is None
                    and match.status is FileProcessingStatus.COMPLETED
                ),
                None,
            )
            if active_match is not None:
                return UploadFileResult(
                    index=position,
                    filename=filename,
                    status="duplicate",
                    file_id=active_match.file_id,
                    doc_id=active_match.doc_id,
                    detail=f"Identical content already uploaded as {active_match.filename!r}",
                )
            trashed_match = next(
                (
                    match
                    for match in matches
                    if match.trashed_at is not None and match.doc_id
                ),
                None,
            )
            if trashed_match is not None and trashed_match.trashed_at is not None:
                expired = datetime.now(UTC) - trashed_match.trashed_at > timedelta(
                    seconds=trash_ttl_seconds
                )
                if not expired:
                    restored = projects.restore_file(
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        file_id=trashed_match.file_id,
                    )
                    if restored is not None:
                        return UploadFileResult(
                            index=position,
                            filename=filename,
                            status="restored",
                            file_id=restored.file_id,
                            doc_id=restored.doc_id,
                            detail=f"Restored {restored.filename!r} from the trash",
                        )
                else:
                    # Past the restore window the vectors are stale by policy:
                    # purge the trashed row and index this upload from scratch.
                    _purge_file(
                        record=trashed_match,
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        projects=projects,
                        hybrid_index=hybrid_index,
                        media_store=media_store,
                    )

            record = projects.add_file(
                owner_user_id=owner_user_id,
                project_id=project_id,
                filename=filename,
                content_hash=content_hash,
            )
            if record is None:
                return failed("Project not found")
            projects.update_file_status(
                owner_user_id=owner_user_id,
                project_id=project_id,
                file_id=record.file_id,
                status=FileProcessingStatus.PROCESSING,
            )

            try:
                parsed = process_document(
                    upload_path,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    source_uri=f"upload://{filename}",
                    scan_parser=scan_parser,
                    converter=converter,
                    media_store=media_store,
                )
                chunked = chunk_document(
                    parsed.document, parsed.blocks, config=chunking
                )
                if media_store is not None:
                    # Written before indexing: an orphaned markdown file after a
                    # failed index is harmless, while an indexed document with
                    # no markdown produces citations that click through to
                    # nothing.
                    markdown_store(parsed.document, parsed.blocks, media_store)
            except Exception:
                _log.exception(
                    "Document processing failed for project=%s file=%s",
                    project_id,
                    filename,
                )
                return failed(
                    "Uploaded document could not be processed",
                    "Document processing failed",
                )

            try:
                hybrid_index.upsert(chunked.chunks, bundle.embedder)
            except ProviderError:
                _log.exception(
                    "Provider request failed while indexing project=%s file=%s",
                    project_id,
                    filename,
                )
                return failed("Provider request failed", "Provider request failed")
            except ValueError:
                _log.exception(
                    "Index configuration failed for project=%s file=%s",
                    project_id,
                    filename,
                )
                return failed(
                    "Index configuration error", "Index configuration failed"
                )
            except Exception:
                _log.exception(
                    "Index request failed for project=%s file=%s",
                    project_id,
                    filename,
                )
                return failed("Index request failed", "Index request failed")

            completed = projects.update_file_status(
                owner_user_id=owner_user_id,
                project_id=project_id,
                file_id=record.file_id,
                status=FileProcessingStatus.COMPLETED,
                doc_id=parsed.document.doc_id,
            )
            if completed is None:
                return failed(
                    "File status could not be updated", "Upload processing failed"
                )

            response = ProcessDocumentResponse(
                file_id=completed.file_id,
                doc_id=parsed.document.doc_id,
                filename=filename,
                source_type=parsed.document.source_type.value,
                block_count=len(parsed.blocks),
                chunk_count=len(chunked.chunks),
                indexed_count=len(chunked.chunks),
                status=completed.status,
            )
            return UploadFileResult.completed(position, response)
    except Exception:
        _log.exception(
            "Unexpected upload worker failure for project=%s file=%s",
            project_id,
            filename,
        )
        return failed("Upload processing failed", "Upload processing failed")


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.post(
        "/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED
    )
    def create_project(
        request: CreateProjectRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ProjectView:
        owner_user_id = user_id(x_user_id)
        try:
            bundle = ctx.registry.get(request.provider)
        except ProviderError as error:
            raise HTTPException(status_code=503, detail="Requested provider is unavailable") from error
        created = ctx.projects.create_project(
            owner_user_id=owner_user_id,
            name=request.name,
            embedding_profile=bundle.profile,
            icon=request.icon,
            color=request.color,
        )
        # Brand new, so nobody has been added yet.
        return ProjectView(**created.model_dump(), role=ProjectRole.OWNER, member_count=0)

    @app.get("/projects", response_model=list[ProjectView])
    def list_projects(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> list[ProjectView]:
        """Projects the caller owns, plus the ones shared with their address.

        One list rather than two endpoints: the sidebar needs every project the
        caller can open, and the "Shared with you" screen splits the same rows
        by role.
        """
        caller = user_id(x_user_id)
        mine = ctx.projects.list_projects(owner_user_id=caller)
        email = authenticated_email()
        shared = (
            [
                project
                for project in ctx.projects.list_shared_projects(email=email)
                # Guard against a stale membership row for a project the caller
                # has since come to own; it would otherwise appear twice.
                if project.owner_user_id != caller
            ]
            if email is not None
            else []
        )
        # One grouped query for the whole page rather than one per row.
        counts = ctx.projects.member_counts(
            project_ids=[project.project_id for project in [*mine, *shared]]
        )
        return [
            ProjectView(
                **project.model_dump(),
                role=ProjectRole.OWNER,
                member_count=counts.get(project.project_id, 0),
            )
            for project in mine
        ] + [
            ProjectView(
                **project.model_dump(),
                role=ctx.projects.member_role(
                    project_id=project.project_id, email=email or ""
                )
                or ProjectRole.VIEWER,
                member_count=counts.get(project.project_id, 0),
            )
            for project in shared
        ]

    # --- members -------------------------------------------------------------

    def owner_only(caller: str, project_id: str) -> Project:
        """The powers a member never gets: deleting the project, managing the
        member list, and handing it over.

        Goes through `accessible_project` rather than `owned_project` so a
        member gets a 403 that says why, and only an outsider gets a 404. Every
        other role-gated route answers the same way."""
        project, _role = ctx.accessible_project(
            caller, project_id, need=ProjectRole.OWNER
        )
        return project

    @app.get(
        "/projects/{project_id}/members", response_model=list[ProjectMemberView]
    )
    def list_members(
        project_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> list[ProjectMemberView]:
        # Any member may see who else is on the project; keeping the list from
        # them would make a shared project feel like a black box.
        ctx.accessible_project(user_id(x_user_id), project_id)
        return [
            ProjectMemberView(email=member.email, role=member.role)
            for member in ctx.projects.list_members(project_id=project_id)
        ]

    @app.post(
        "/projects/{project_id}/members",
        response_model=list[ProjectMemberView],
        status_code=status.HTTP_201_CREATED,
    )
    def add_member(
        project_id: str,
        request: ProjectMemberRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> list[ProjectMemberView]:
        caller = user_id(x_user_id)
        # Checked before ownership so an anonymous owner is told to log in
        # rather than that their own project does not exist. Matching an
        # invitation needs a verified address, which needs an account.
        if is_anonymous_id(caller):
            raise HTTPException(
                status_code=403, detail="login_required_to_share_project"
            )
        owner_only(caller, project_id)
        email = normalize_email(request.email)
        if email == authenticated_email():
            raise HTTPException(
                status_code=422, detail="You already own this project"
            )
        members = ctx.projects.add_member(
            project_id=project_id, email=email, role=ProjectRole(request.role)
        )
        return [
            ProjectMemberView(email=member.email, role=member.role)
            for member in members
        ]

    @app.delete(
        "/projects/{project_id}/members", status_code=status.HTTP_204_NO_CONTENT
    )
    def remove_member(
        project_id: str,
        request: RemoveProjectMemberRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        owner_only(user_id(x_user_id), project_id)
        if not ctx.projects.remove_member(
            project_id=project_id, email=request.email
        ):
            raise HTTPException(status_code=404, detail="Not a member")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/projects/{project_id}/transfer", response_model=ProjectView)
    def transfer_project(
        project_id: str,
        request: TransferProjectRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ProjectView:
        """Hand the project to somebody else and stay on as an editor.

        The target has to have logged in at least once: ownership is stored as
        a uid, and there is nobody to give a project to until an address has
        one.
        """
        caller = user_id(x_user_id)
        project = owner_only(caller, project_id)
        current_email = authenticated_email()
        if current_email is None:
            raise HTTPException(
                status_code=403, detail="login_required_to_share_project"
            )
        target_email = normalize_email(request.email)
        if target_email == current_email:
            raise HTTPException(status_code=422, detail="You already own this project")
        if ctx.users is None:
            raise HTTPException(
                status_code=503, detail="User registry is unavailable"
            )
        new_owner = ctx.users.find_uid_by_email(target_email)
        if new_owner is None:
            raise HTTPException(
                status_code=404, detail="No account uses that address yet"
            )
        transferred = ctx.projects.transfer_owner(
            project_id=project_id,
            current_owner_user_id=caller,
            current_owner_email=current_email,
            new_owner_user_id=new_owner,
            new_owner_email=target_email,
        )
        if transferred is None:
            raise HTTPException(status_code=404, detail="Project not found")
        # The chunks still carry the previous owner's tenant, and every read
        # runs as the project's current owner. Without this the project would
        # silently stop finding its own documents.
        bundle = ctx.project_bundle(project)
        QdrantHybridIndex(
            ctx.qdrant, bundle.collection_name, bundle.profile
        ).reassign_project_owner(
            project_id=project_id,
            previous_owner_user_id=caller,
            new_owner_user_id=new_owner,
        )
        if ctx.retrieval_cache is not None:
            ctx.retrieval_cache.invalidate_project(project_id)
        return ProjectView(
            **transferred.model_dump(),
            role=ProjectRole.EDITOR,
            member_count=len(ctx.projects.list_members(project_id=project_id)),
        )

    @app.patch("/projects/{project_id}", response_model=ProjectView)
    def rename_project(
        project_id: str,
        request: RenameProjectRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Project:
        project, role = ctx.accessible_project(
            user_id(x_user_id), project_id, need=ProjectRole.EDITOR
        )
        renamed = ctx.projects.rename_project(
            owner_user_id=project.owner_user_id,
            project_id=project_id,
            name=request.name,
        )
        if renamed is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return ProjectView(
            **renamed.model_dump(),
            role=role,
            member_count=len(ctx.projects.list_members(project_id=project_id)),
        )

    @app.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_project(
        project_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        project = owner_only(user_id(x_user_id), project_id)
        owner_user_id = project.owner_user_id
        bundle = ctx.project_bundle(project)
        records = (
            ctx.projects.list_files(
                owner_user_id=owner_user_id,
                project_id=project_id,
                include_trashed=True,
            )
            or []
        )
        # Vectors before the Postgres row: if any cleanup below fails, the row
        # survives and a retry can still locate the project. The reverse order
        # would orphan the vectors with nothing left pointing at them.
        index = QdrantHybridIndex(ctx.qdrant, bundle.collection_name, bundle.profile)
        index.delete_by_project(
            owner_user_id=owner_user_id, project_id=project_id
        )
        if ctx.media_store is not None:
            for record in records:
                if not record.doc_id:
                    continue
                try:
                    ctx.media_store.delete_prefix(record.doc_id)
                except OSError as error:
                    _log.warning(
                        "Media cleanup failed for doc=%s in project=%s: %s",
                        record.doc_id,
                        project_id,
                        error,
                    )
        if ctx.retrieval_cache is not None:
            ctx.retrieval_cache.invalidate_project(project_id)
        deleted = ctx.projects.delete_project(
            owner_user_id=owner_user_id, project_id=project_id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Project not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/projects/{project_id}/documents", response_model=list[ProjectFile])
    def list_documents(
        project_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> list[ProjectFile]:
        project, _role = ctx.accessible_project(user_id(x_user_id), project_id)
        records = ctx.projects.list_files(
            owner_user_id=project.owner_user_id, project_id=project_id
        )
        return records or []

    @app.delete(
        "/projects/{project_id}/documents/{file_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_document(
        project_id: str,
        file_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        permanent: bool = False,
    ) -> Response:
        project, _role = ctx.accessible_project(
            user_id(x_user_id), project_id, need=ProjectRole.EDITOR
        )
        owner_user_id = project.owner_user_id
        record = ctx.projects.get_file(
            owner_user_id=owner_user_id, project_id=project_id, file_id=file_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Document not found")
        # Rows without an indexed document (pending/failed uploads) have no
        # vectors worth keeping, so the trash would only preserve a broken row:
        # they are purged outright even without `permanent`.
        if permanent or record.doc_id is None:
            bundle = ctx.project_bundle(project)
            hybrid_index = QdrantHybridIndex(
                ctx.qdrant, bundle.collection_name, bundle.profile
            )
            _purge_file(
                record=record,
                owner_user_id=owner_user_id,
                project_id=project_id,
                projects=ctx.projects,
                hybrid_index=hybrid_index,
                media_store=ctx.media_store,
            )
        else:
            if record.trashed_at is None:
                ctx.projects.trash_file(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    file_id=file_id,
                )
        if ctx.retrieval_cache is not None:
            ctx.retrieval_cache.invalidate_project(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/projects/{project_id}/documents/{doc_id}/markdown",
        response_model=DocumentMarkdownResponse,
    )
    def document_markdown(
        project_id: str,
        doc_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        response: Response,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> DocumentMarkdownResponse | Response:
        ctx.owned_document(user_id(x_user_id), project_id, doc_id)
        if ctx.media_store is None:
            raise HTTPException(status_code=404, detail="Document markdown not found")
        markdown_ref, anchors_ref = markdown_keys(doc_id)
        try:
            markdown = ctx.media_store.get(markdown_ref)
            anchors = json.loads(ctx.media_store.get(anchors_ref))
        except (OSError, ValueError):
            # Documents indexed before this feature existed have no sidecar,
            # and re-rendering them is impossible: their blocks are gone.
            raise HTTPException(
                status_code=404, detail="Document markdown not found"
            ) from None

        # Hashed from the bytes rather than reused from `doc_id`, which only
        # identifies the source file and would not change if the renderer did.
        etag = f'"{hashlib.sha256(markdown).hexdigest()[:32]}"'
        if if_none_match == etag:
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag}
            )
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return DocumentMarkdownResponse(
            doc_id=doc_id, markdown=markdown.decode(), anchors=anchors
        )

    @app.get("/projects/{project_id}/media/{key:path}")
    def document_media(
        project_id: str,
        key: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        # The key is attacker-supplied, so its shape is checked here rather than
        # leaving containment to the store: an object-storage backend has no
        # filesystem root to be relative to.
        segments = key.split("/")
        if len(segments) != 3 or any(
            not segment or segment in {".", ".."} or "\\" in segment
            for segment in segments
        ):
            raise HTTPException(status_code=404, detail="Media not found")
        ctx.owned_document(user_id(x_user_id), project_id, segments[0])
        if ctx.media_store is None:
            raise HTTPException(status_code=404, detail="Media not found")
        try:
            data = ctx.media_store.get(key)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Media not found") from None
        return Response(
            content=data,
            media_type=content_type_for(Path(key)),
            # The filename carries a content digest, so a stored object never
            # changes under its key.
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.post(
        "/projects/{project_id}/documents",
    )
    def process_upload(
        project_id: str,
        files: Annotated[
            list[UploadFile],
            File(description="PDF, DOCX, XLSX, PPTX, or an image (PNG/JPEG/TIFF/WEBP)"),
        ],
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> StreamingResponse:
        project, _role = ctx.accessible_project(
            user_id(x_user_id), project_id, need=ProjectRole.EDITOR
        )
        # Indexed under the project's owner so the whole team can retrieve it.
        owner_user_id = project.owner_user_id
        bundle = ctx.project_bundle(project)
        if not files:
            raise HTTPException(status_code=422, detail="At least one file is required")
        if len(files) > ctx.upload.max_files_per_batch:
            raise HTTPException(
                status_code=413,
                detail=f"Batch exceeds the {ctx.upload.max_files_per_batch}-file limit",
            )
        batch_bytes = sum(_upload_size(file) for file in files)
        if batch_bytes > ctx.upload.max_batch_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Batch exceeds the {ctx.upload.max_batch_bytes}-byte limit",
            )

        hybrid_index = QdrantHybridIndex(
            ctx.qdrant,
            bundle.collection_name,
            bundle.profile,
        )
        has_processable_file = False
        for file in files:
            filename = Path(file.filename or "").name
            size = _upload_size(file)
            if not filename or size == 0 or size > ctx.upload.max_file_bytes:
                continue
            try:
                route_file(Path(filename))
            except ValueError:
                continue
            has_processable_file = True
            break
        if has_processable_file:
            try:
                hybrid_index.ensure_collection()
            except ValueError as error:
                _log.exception(
                    "Index configuration failed for project=%s",
                    project_id,
                )
                raise HTTPException(
                    status_code=503, detail="Index configuration error"
                ) from error
            except Exception as error:
                _log.exception(
                    "Index initialization failed for project=%s",
                    project_id,
                )
                raise HTTPException(status_code=503, detail="Index request failed") from error

        def emit() -> Iterator[str]:
            futures: dict[Future[UploadFileResult], tuple[int, UploadFile]] = {
                ctx.upload_executor.submit(
                    _process_one_upload,
                    position=position,
                    file=file,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    projects=ctx.projects,
                    hybrid_index=hybrid_index,
                    bundle=bundle,
                    max_upload_bytes=ctx.upload.max_file_bytes,
                    chunking=ctx.chunking,
                    scan_parser=ctx.scan_parser,
                    converter=ctx.converter,
                    media_store=ctx.media_store,
                    trash_ttl_seconds=ctx.upload.trash_ttl_seconds,
                ): (position, file)
                for position, file in enumerate(files)
            }
            completed_count = 0
            failed_count = 0
            duplicate_count = 0
            restored_count = 0
            invalidated = False
            try:
                for future in as_completed(futures):
                    position, file = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        filename = Path(file.filename or "").name
                        _log.exception(
                            "Upload future failed for project=%s file=%s",
                            project_id,
                            filename,
                        )
                        result = UploadFileResult(
                            index=position,
                            filename=filename,
                            status="failed",
                            detail="Upload processing failed",
                        )
                    if result.status == "completed":
                        completed_count += 1
                    elif result.status == "duplicate":
                        duplicate_count += 1
                    elif result.status == "restored":
                        restored_count += 1
                    else:
                        failed_count += 1
                    yield sse("file_result", result.model_dump(mode="json"))
                # Before `done`, and only when something was actually indexed:
                # a cached outcome computed a moment ago cannot see the document
                # that just landed, and would answer with a confident citation to
                # the older evidence. Ordering it ahead of `done` means a client
                # that queries the instant it sees `done` already reads through
                # the bumped version. A restore changes the retrievable set the
                # same way, so it bumps too; a duplicate changes nothing.
                if ctx.retrieval_cache is not None and (
                    completed_count or restored_count
                ):
                    ctx.retrieval_cache.invalidate_project(project_id)
                    invalidated = True
                yield sse(
                    "done",
                    {
                        "total": len(files),
                        "completed": completed_count,
                        "failed": failed_count,
                        "duplicates": duplicate_count,
                        "restored": restored_count,
                    },
                )
            finally:
                for future in futures:
                    future.cancel()
                # A disconnect closes this generator, not the executor: `cancel`
                # only stops futures that have not started, so files already in
                # flight index to completion with nobody left to bump the cache
                # version -- the loop above died at a yield, and its
                # `completed_count` froze with it. Waiting costs nothing a
                # connected client would not also have paid, and it lets the
                # bump read the real results instead of that frozen count.
                if ctx.retrieval_cache is not None and not invalidated:
                    wait(list(futures))
                    if any(_upload_completed(future) for future in futures):
                        ctx.retrieval_cache.invalidate_project(project_id)

        return StreamingResponse(
            emit(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/projects/{project_id}/query", response_model=QueryResponse)
    def query(
        project_id: str,
        request: QueryRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> QueryResponse:
        project, _role = ctx.accessible_project(user_id(x_user_id), project_id)
        owner_user_id = project.owner_user_id
        outcome = ctx.retrieve(request, owner_user_id=owner_user_id, project=project)
        return to_response(request, outcome)

    @app.post("/projects/{project_id}/answer", response_model=AnswerResponse)
    def answer(
        project_id: str,
        request: QueryRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> AnswerResponse:
        project, _role = ctx.accessible_project(user_id(x_user_id), project_id)
        owner_user_id = project.owner_user_id
        outcome = ctx.retrieve(request, owner_user_id=owner_user_id, project=project)
        response = to_response(request, outcome)
        generated = ctx.answerer.answer(
            request.query,
            context_evidence(outcome),
            # No thread here (stateless one-shot route); the project is the
            # next best correlation id for spreading load without splitting
            # a single conversation across accounts.
            sticky_key=project_id,
        )
        return AnswerResponse(
            **response.model_dump(),
            answer=generated.text,
            # Leaf-level, not the merged blocks the prompt saw: citations must
            # point at citable chunks, and every leaf here is covered by them.
            citations=[hit.chunk_id for hit in response.hits],
        )

