"""Contracts for the thread-summary delta.

The summary is a list of items the *code* owns; the model only ever proposes
changes to it. `SummaryItem` is the code's own record and forbids surprises;
`SummaryOp` and `SummaryDelta` are model output and tolerate them, the same
split `schemas.understanding` makes for the same reason.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from rag_ingestion.config import DEFAULT_SUMMARY_ITEM_MAX_CHARS
from rag_ingestion.models import StrictModel


class SummaryOpKind(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DROP = "DROP"


class SummaryItem(StrictModel):
    """One fact in the running summary, with an id that outlives every fold.

    The id is what lets the model name an item it wants gone. An item nobody
    names survives untouched, which is the whole inversion this design makes:
    forgetting now costs a word, remembering costs nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^s[1-9][0-9]*$")
    text: str = Field(min_length=1, max_length=DEFAULT_SUMMARY_ITEM_MAX_CHARS)
    #: How often an answer said it leaned on this item, and how many folds it
    #: has survived. Eviction ranks on `used / (folds + 1)`, so an item that
    #: keeps earning its place outlives a newer one nobody has ever used.
    #: Both zero on items written before counters existed.
    used: int = Field(default=0, ge=0)
    folds: int = Field(default=0, ge=0)

    def older(self) -> SummaryItem:
        """One more fold survived."""
        return self.model_copy(update={"folds": self.folds + 1})

    def more_used(self) -> SummaryItem:
        """One more answer said it leaned on this item."""
        return self.model_copy(update={"used": self.used + 1})


class SummaryOp(StrictModel):
    """One change the model asks for.

    Model output, so extra keys are ignored rather than fatal: a model that
    invents a `reason` field should not lose its operation over it.
    """

    model_config = ConfigDict(extra="ignore")

    op: SummaryOpKind
    #: Required for UPDATE and DROP. Ignored on ADD: ids are assigned by the
    #: merge, never by the model, so a guessed id cannot overwrite an item.
    id: str | None = None
    #: Required for ADD and UPDATE. Truncated rather than rejected at the
    #: length cap: the fact is right, only its size is wrong.
    text: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> SummaryOp:
        if self.op is not SummaryOpKind.ADD and not self.id:
            raise ValueError(f"{self.op.value} needs the id of an existing item")
        if self.op is not SummaryOpKind.DROP and not (self.text or "").strip():
            raise ValueError(f"{self.op.value} needs text")
        return self


class SummaryDelta(StrictModel):
    """The whole response, documented as a contract.

    `parse_delta` deliberately does not validate through this model: it walks
    the ops one at a time so a single malformed operation cannot discard the
    others, the same way `understanding._parse` keeps its good candidates.
    """

    model_config = ConfigDict(extra="ignore")

    ops: list[SummaryOp] = Field(default_factory=list)
