from typing import Protocol

from pydantic import Field

from rag_ingestion.models import StrictModel


class RankedResult(StrictModel):
    index: int = Field(ge=0)
    relevance_score: float


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[RankedResult]: ...
