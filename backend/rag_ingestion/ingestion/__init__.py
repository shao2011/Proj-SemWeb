"""Step 1: turn a source file into a normalized Document plus atomic Block[]."""

from rag_ingestion.ingestion.pipeline import process_document
from rag_ingestion.ingestion.reconciliation import reconcile_blocks
from rag_ingestion.ingestion.router import ParseRoute, ParserKind, route_file

__all__ = [
    "ParseRoute",
    "ParserKind",
    "process_document",
    "reconcile_blocks",
    "route_file",
]
