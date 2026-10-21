from pydantic import Field

from rag_ingestion.models import StrictModel


class AnswerEvidence(StrictModel):
    chunk_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    breadcrumb: str = ""


class Answer(StrictModel):
    text: str = Field(min_length=1)
    citations: list[str]
