from typing import Literal

from pydantic import ConfigDict, Field

from rag_ingestion.models import StrictModel
from rag_ingestion.memory.models import MemoryType


class MemoryCandidate(StrictModel):
    """One thing worth remembering, as the extractor sees it.

    There is deliberately no scope here. The field used to exist and was not
    dependable: the same sentence run through UnderstandMessage three times came
    back as `user` once and `thread` once. Scope is decided from `key` in
    `memory_routing`, which leaves the model only the part it is good at.

    Unlike the rest of the codebase this tolerates extra keys instead of
    forbidding them. The payload is model output, not an internal contract, and a
    model that still emits the removed `suggested_scope` out of habit would
    otherwise have its whole candidate thrown away by `_parse`.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal[MemoryType.FACT, MemoryType.PREFERENCE]
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class Understanding(StrictModel):
    rewritten_query: str | None = None
    memory: list[MemoryCandidate] = Field(default_factory=list, max_length=3)
