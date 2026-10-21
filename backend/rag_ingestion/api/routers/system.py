"""System routes: health, per-user settings, and LLM usage."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header

from rag_ingestion.memory.models import LlmUsage, UserSettings
from rag_ingestion.api.deps import ApiContext, user_id
from rag_ingestion.api.schemas import (
    HealthResponse,
    ModelCatalogResponse,
    ModelOption,
    UpdateSettingsRequest,
)


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", configured_providers=ctx.registry.providers)

    @app.get("/models", response_model=ModelCatalogResponse)
    def list_models() -> ModelCatalogResponse:
        """The picker's list. Ids and labels only: budgets stay server-side,
        where the gauge percentage is computed.

        Disabled specs, and specs whose router holds no keys, are filtered
        here rather than out of the catalog: a thread that recorded one
        still has to resolve to its own budget.
        """
        return ModelCatalogResponse(
            default=ctx.models.default,
            models=[
                ModelOption(id=spec.id, label=spec.label)
                for spec in ctx.models.models
                if spec.enabled
                and (ctx.routers is None or spec.router in ctx.routers)
            ],
        )

    @app.get("/settings", response_model=UserSettings)
    def get_settings(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> UserSettings:
        return ctx.memory_store().get_settings(owner_user_id=user_id(x_user_id))

    @app.patch("/settings", response_model=UserSettings)
    def update_settings(
        request: UpdateSettingsRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> UserSettings:
        store = ctx.memory_store()
        owner = user_id(x_user_id)
        if request.memory_enabled is not None:
            store.set_memory_enabled(
                owner_user_id=owner, enabled=request.memory_enabled
            )
        if request.display_name is not None or request.email is not None:
            store.update_profile(
                owner_user_id=owner,
                display_name=request.display_name,
                email=request.email,
            )
        return store.get_settings(owner_user_id=owner)

    @app.get("/usage", response_model=LlmUsage)
    def get_usage(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> LlmUsage:
        return ctx.memory_store().get_llm_usage(owner_user_id=user_id(x_user_id))

