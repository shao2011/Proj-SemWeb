"""Contracts for the chat-model catalog (`rag_ingestion/data/models.json`).

One entry per model a user can pick in the chat UI. The numbers here feed the
context gauge and the summary trigger, so they are validated hard: a spec
whose output cap swallows its context window would make the gauge's
denominator zero and the trigger fire on every turn.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from rag_ingestion.models import StrictModel


class ModelSpec(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=64)
    #: Total window, input + output together, as published by the provider.
    context_tokens: int = Field(gt=0)
    #: Passed as `max_tokens` on generation. Counts against `context_tokens`,
    #: which is why the gauge's usable budget subtracts it.
    max_output_tokens: int = Field(gt=0)
    #: Ceiling on verbatim-replayed messages, sized to the model's window.
    #: The read limit on the message store is derived from this.
    keep_recent_max_messages: int = Field(ge=6)
    #: Reasoning effort, for models that accept one. Stored but not yet sent
    #: to the provider; nullable because e.g. Haiku 4.5 rejects the parameter.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    #: Whether the picker offers this model. False hides it from `/models`
    #: while `resolve` still finds it, so a thread that already recorded the
    #: id keeps its own budget instead of silently inheriting the default's.
    enabled: bool = True
    #: Which gateway serves this model, by the name configured in
    #: `LLMConfig.routers`. Deliberately not called `provider`: that word is
    #: taken by LangChain's `model_provider` (the wire format), and every
    #: router here speaks the same one.
    router: str = Field(default="openrouter", min_length=1)

    @model_validator(mode="after")
    def validate_output_leaves_input_room(self) -> ModelSpec:
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError(
                f"{self.id}: max_output_tokens ({self.max_output_tokens}) must be "
                f"smaller than context_tokens ({self.context_tokens})"
            )
        return self


class ModelCatalog(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    default: str = Field(min_length=1)
    models: list[ModelSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> ModelCatalog:
        ids = [spec.id for spec in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("Model ids must be unique")
        if self.default not in ids:
            raise ValueError(f"Default {self.default!r} is not in the catalog")
        return self

    def resolve(self, model_id: str | None) -> ModelSpec:
        """The spec for `model_id`, or the default when it is unknown.

        Falls back rather than raising: a thread that recorded a model later
        removed from the catalog must keep answering on the default, not
        crash its next turn. The caller logs the substitution.
        """
        for spec in self.models:
            if spec.id == model_id:
                return spec
        return next(spec for spec in self.models if spec.id == self.default)
