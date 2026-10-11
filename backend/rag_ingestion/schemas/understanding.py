from typing import Literal

from pydantic import ConfigDict, Field

from rag_ingestion.models import StrictModel
from rag_ingestion.memory.models import MemoryType


class MemoryCandidate(StrictModel):
    """One thing worth remembering, as the extractor sees it.

    There is deliberately no scope here: a model's guess at scope is not
    dependable, so scope is decided elsewhere.

    Unlike the rest of the codebase this tolerates extra keys instead of
    forbidding them. The payload is model output, not an internal contract, and
    a model that emits an unexpected field such as `suggested_scope` would
    otherwise have its whole candidate thrown away by `_parse`.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal[MemoryType.FACT, MemoryType.PREFERENCE]
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class Understanding(StrictModel):
    rewritten_query: str | None = None
    memory: list[MemoryCandidate] = Field(default_factory=list, max_length=3)
