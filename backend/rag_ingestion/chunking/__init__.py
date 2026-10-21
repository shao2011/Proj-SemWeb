"""Step 2: structure detection and chunk construction."""

from rag_ingestion.chunking.chunker import (
    Chunk,
    ChunkingConfig,
    ChunkingResult,
    ChunkLevel,
    chunk_document,
)
from rag_ingestion.chunking.structure import (
    NaturalUnit,
    StructureDetection,
    assign_clause_sections,
    detect_natural_unit,
)

__all__ = [
    "Chunk",
    "ChunkLevel",
    "ChunkingConfig",
    "ChunkingResult",
    "NaturalUnit",
    "StructureDetection",
    "assign_clause_sections",
    "chunk_document",
    "detect_natural_unit",
]
