"""Everything memory: contracts, storage, scope routing, and the LLM tools.

Split by role rather than by layer, because these four move together:

  models.py   -- Memory, scopes, caps, the closed key list, the store Protocol
  store.py    -- the Postgres implementation
  routing.py  -- value comparison, and when a local memory is really a habit
  tools.py    -- the tools the answering model calls to save memory inline
"""

from rag_ingestion.memory.models import (
    MEMORY_CAPS,
    Memory,
    MemoryAuditEntry,
    LlmUsage,
    MemoryKey,
    MemoryOperation,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    MemoryStore,
    MemoryType,
    UserSettings,
    is_valid_key,
)
from rag_ingestion.memory.routing import normalize, should_promote
from rag_ingestion.memory.store import PostgresMemoryStore
from rag_ingestion.memory.tools import MemoryWriter, build_memory_tools, normalize_key

__all__ = [
    "MEMORY_CAPS",
    "Memory",
    "MemoryAuditEntry",
    "LlmUsage",
    "MemoryKey",
    "MemoryOperation",
    "MemoryProposal",
    "MemoryScope",
    "MemorySource",
    "MemoryStore",
    "MemoryType",
    "MemoryWriter",
    "PostgresMemoryStore",
    "UserSettings",
    "build_memory_tools",
    "is_valid_key",
    "normalize_key",
    "normalize",
    "should_promote",
]
