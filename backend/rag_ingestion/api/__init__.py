"""HTTP boundary: the FastAPI app factory and its routes."""

from rag_ingestion.api.app import RuntimeConfig, create_runtime_app
from rag_ingestion.api.routes import create_query_app
from rag_ingestion.api.schemas import (
    AnswerResponse,
    ProcessDocumentResponse,
    QueryHit,
    QueryRequest,
    QueryResponse,
    UploadFileResult,
)

__all__ = [
    "AnswerResponse",
    "ProcessDocumentResponse",
    "QueryHit",
    "QueryRequest",
    "QueryResponse",
    "RuntimeConfig",
    "UploadFileResult",
    "create_query_app",
    "create_runtime_app",
]
