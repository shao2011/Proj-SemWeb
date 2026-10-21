import pytest
from pydantic import ValidationError
from sqlalchemy import Engine

from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.projects import FileProcessingStatus, ProjectStore
from rag_ingestion.indexing.qdrant_index import EmbeddingProfile


@pytest.fixture
def profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="cohere",
        model="embed-v4.0",
        version="2026-08",
        dimensions=1024,
        task="search",
        mode="naive",
    )


@pytest.fixture
def store(project_store: PostgresProjectStore) -> PostgresProjectStore:
    return project_store


def test_projects_are_persistent_profile_pinned_and_owner_scoped(
    pg_engine: Engine, profile: EmbeddingProfile
) -> None:
    store = PostgresProjectStore(pg_engine)
    project = store.create_project(
        owner_user_id="user-a",
        name="Private research",
        embedding_profile=profile,
    )
    file_record = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="persistent.pdf",
        content_hash="0" * 64,
    )
    assert isinstance(store, ProjectStore)
    assert file_record is not None
    original_model = project.embedding_profile.model
    profile.model = "mutated-after-create"

    assert project.owner_user_id == "user-a"
    assert project.embedding_profile.model == original_model
    with pytest.raises(ValidationError, match="frozen"):
        project.embedding_profile.model = "cannot-change-pinned-profile"
    assert store.get_project(
        owner_user_id="user-b", project_id=project.project_id
    ) is None
    assert store.list_projects(owner_user_id="user-b") == []

    reopened = PostgresProjectStore(pg_engine)
    persisted = reopened.get_project(
        owner_user_id="user-a", project_id=project.project_id
    )
    assert persisted == project
    assert reopened.list_projects(owner_user_id="user-a") == [project]
    assert reopened.list_files(
        owner_user_id="user-a", project_id=project.project_id
    ) == [file_record]


def test_project_supports_multiple_owner_scoped_file_records(
    store: PostgresProjectStore, profile: EmbeddingProfile
) -> None:
    project = store.create_project(
        owner_user_id="user-a",
        name="Evidence",
        embedding_profile=profile,
    )
    assert store.list_files(
        owner_user_id="user-a", project_id=project.project_id
    ) == []

    first = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="policy.pdf",
        content_hash="a" * 64,
    )
    second = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="appendix.docx",
        content_hash="b" * 64,
    )

    assert first is not None
    assert second is not None
    assert first.file_id != second.file_id
    assert first.status is FileProcessingStatus.PENDING
    assert store.list_files(
        owner_user_id="user-a", project_id=project.project_id
    ) == [first, second]
    assert store.get_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=first.file_id,
    ) == first
    assert store.list_files(
        owner_user_id="user-b", project_id=project.project_id
    ) is None
    assert store.get_file(
        owner_user_id="user-b",
        project_id=project.project_id,
        file_id=first.file_id,
    ) is None
    assert store.add_file(
        owner_user_id="user-b",
        project_id=project.project_id,
        filename="intrusion.pdf",
        content_hash="c" * 64,
    ) is None


def test_file_status_transitions_record_success_and_failure(
    store: PostgresProjectStore, profile: EmbeddingProfile
) -> None:
    project = store.create_project(
        owner_user_id="user-a",
        name="Processing",
        embedding_profile=profile,
    )
    successful = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="success.pdf",
        content_hash="d" * 64,
    )
    failing = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="failure.pdf",
        content_hash="e" * 64,
    )
    assert successful is not None
    assert failing is not None

    processing = store.update_file_status(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=successful.file_id,
        status=FileProcessingStatus.PROCESSING,
    )
    assert processing is not None
    completed = store.update_file_status(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=successful.file_id,
        status=FileProcessingStatus.COMPLETED,
        doc_id="doc_success",
    )
    assert completed is not None
    assert completed.status is FileProcessingStatus.COMPLETED
    assert completed.doc_id == "doc_success"
    assert completed.error is None
    assert completed.created_at == successful.created_at
    assert completed.updated_at >= successful.updated_at

    store.update_file_status(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=failing.file_id,
        status=FileProcessingStatus.PROCESSING,
    )
    failed = store.update_file_status(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=failing.file_id,
        status=FileProcessingStatus.FAILED,
        error="parser rejected the document",
    )
    assert failed is not None
    assert failed.status is FileProcessingStatus.FAILED
    assert failed.error == "parser rejected the document"

    with pytest.raises(ValueError, match="Invalid file status transition"):
        store.update_file_status(
            owner_user_id="user-a",
            project_id=project.project_id,
            file_id=successful.file_id,
            status=FileProcessingStatus.PROCESSING,
        )


def test_renaming_a_project_is_owner_scoped_and_persists(
    store: PostgresProjectStore, profile: EmbeddingProfile
) -> None:
    project = store.create_project(
        owner_user_id="user-a",
        name="Original",
        embedding_profile=profile,
    )

    assert store.rename_project(
        owner_user_id="user-b", project_id=project.project_id, name="Hijacked"
    ) is None

    renamed = store.rename_project(
        owner_user_id="user-a", project_id=project.project_id, name="Renamed"
    )
    assert renamed is not None
    assert renamed.name == "Renamed"
    assert renamed.updated_at >= project.updated_at

    reloaded = store.get_project(owner_user_id="user-a", project_id=project.project_id)
    assert reloaded is not None
    assert reloaded.name == "Renamed"

    assert store.rename_project(
        owner_user_id="user-a", project_id="prj_missing", name="Nope"
    ) is None


def test_deleting_a_project_removes_its_files(
    store: PostgresProjectStore, profile: EmbeddingProfile
) -> None:
    project = store.create_project(
        owner_user_id="user-a",
        name="Disposable",
        embedding_profile=profile,
    )
    store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="policy.pdf",
        content_hash="a" * 64,
    )

    assert store.delete_project(
        owner_user_id="user-b", project_id=project.project_id
    ) is False
    assert store.get_project(
        owner_user_id="user-a", project_id=project.project_id
    ) is not None

    assert store.delete_project(
        owner_user_id="user-a", project_id=project.project_id
    ) is True
    assert store.get_project(
        owner_user_id="user-a", project_id=project.project_id
    ) is None
    assert store.list_files(
        owner_user_id="user-a", project_id=project.project_id
    ) is None
    assert store.delete_project(
        owner_user_id="user-a", project_id=project.project_id
    ) is False


def test_foreign_owner_cannot_transition_file_status(
    store: PostgresProjectStore, profile: EmbeddingProfile
) -> None:
    project = store.create_project(
        owner_user_id="user-a",
        name="Private processing",
        embedding_profile=profile,
    )
    file_record = store.add_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        filename="private.pdf",
        content_hash="f" * 64,
    )
    assert file_record is not None

    assert store.update_file_status(
        owner_user_id="user-b",
        project_id=project.project_id,
        file_id=file_record.file_id,
        status=FileProcessingStatus.PROCESSING,
    ) is None
    assert store.get_file(
        owner_user_id="user-a",
        project_id=project.project_id,
        file_id=file_record.file_id,
    ) == file_record
