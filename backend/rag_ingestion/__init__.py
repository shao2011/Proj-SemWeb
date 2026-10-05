"""RAG ingestion and retrieval package.

Layout follows the pipeline:

    models        shared Document / Block / Locator contracts
    config        every runtime setting, read from env then .env
    chat          ChatTurn and the ChatCompleter contract
    ingestion/    parse and reconcile a source file into Block[]
    chunking/     structure detection and chunk construction
    indexing/     embedding contracts and the hybrid Qdrant index
    retrieval/    the query chain and answer assembly
    providers/    vendor-neutral embedder and reranker bundles
    storage/      project metadata, threads, and media storage
    api/          FastAPI app factory and routes
"""

from rag_ingestion.chunking import (
    Chunk,
    ChunkingConfig,
    ChunkingResult,
    ChunkLevel,
    NaturalUnit,
    chunk_document,
    detect_natural_unit,
)
from rag_ingestion.indexing import (
    Embedder,
    EmbeddingProfile,
    LocalHashEmbedder,
    QdrantHybridIndex,
)
from rag_ingestion.ingestion import ParserKind, process_document, route_file
from rag_ingestion.models import Block, Document, ParseResult
from rag_ingestion.providers.registry import Provider, ProviderBundle, ProviderRegistry
from rag_ingestion.retrieval import (
    Answer,
    AnswerEvidence,
    MergedContext,
    MockAnswerer,
    RankedResult,
    RetrievalConfig,
    RetrievalOutcome,
    auto_merge,
    run_retrieval,
)
from rag_ingestion.storage import (
    FileProcessingStatus,
    LocalMediaStore,
    MediaStore,
    Project,
    ProjectFile,
    PostgresProjectStore,
)

__all__ = [
    "Answer",
    "AnswerEvidence",
    "Block",
    "Chunk",
    "ChunkLevel",
    "ChunkingConfig",
    "ChunkingResult",
    "Document",
    "Embedder",
    "EmbeddingProfile",
    "FileProcessingStatus",
    "LocalHashEmbedder",
    "LocalMediaStore",
    "MediaStore",
    "MergedContext",
    "MockAnswerer",
    "NaturalUnit",
    "ParseResult",
    "ParserKind",
    "Project",
    "ProjectFile",
    "Provider",
    "ProviderBundle",
    "ProviderRegistry",
    "QdrantHybridIndex",
    "RankedResult",
    "RetrievalConfig",
    "RetrievalOutcome",
    "PostgresProjectStore",
    "auto_merge",
    "chunk_document",
    "detect_natural_unit",
    "process_document",
    "route_file",
    "run_retrieval",
]
