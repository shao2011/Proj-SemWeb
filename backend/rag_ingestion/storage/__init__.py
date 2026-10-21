"""Persistence: project metadata, conversation threads, and the media store.

Memory has its own package (`rag_ingestion.memory`) and is deliberately not
re-exported here: two import paths for one contract is how a codebase ends up
with two versions of it.
"""

from rag_ingestion.storage.media import LocalMediaStore, MediaStore, media_key
from rag_ingestion.storage.pg_projects import PostgresProjectStore
from rag_ingestion.storage.pg_threads import PostgresThreadStore
from rag_ingestion.storage.projects import (
    FileProcessingStatus,
    PinnedEmbeddingProfile,
    Project,
    ProjectFile,
    ProjectStore,
)
from rag_ingestion.storage.r2_media import R2MediaStore, build_media_store
from rag_ingestion.storage.threads import (
    Thread,
    ThreadMessage,
    ThreadMessageRole,
    ThreadStore,
)

__all__ = [
    "FileProcessingStatus",
    "LocalMediaStore",
    "MediaStore",
    "PinnedEmbeddingProfile",
    "PostgresProjectStore",
    "PostgresThreadStore",
    "Project",
    "ProjectFile",
    "ProjectStore",
    "R2MediaStore",
    "Thread",
    "ThreadMessage",
    "ThreadMessageRole",
    "ThreadStore",
    "build_media_store",
    "media_key",
]
