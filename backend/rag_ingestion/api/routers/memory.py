"""Memory routes: proposals and the user-visible memory CRUD.

`/memory/proposals` is declared before `/memory/{memory_id}` on purpose:
FastAPI matches in declaration order, so the reverse would make the literal
path be swallowed by the path parameter.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Response, status

from rag_ingestion.memory.models import (
    Memory,
    MemoryProposal,
    MemoryScope,
    MemorySource,
)
from rag_ingestion.api.deps import ApiContext, user_id
from rag_ingestion.api.schemas import CreateMemoryRequest, UpdateMemoryRequest


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.get("/memory/proposals", response_model=list[MemoryProposal])
    def list_memory_proposals(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        limit: int = 50,
    ) -> list[MemoryProposal]:
        return ctx.memory_store().list_proposals(
            owner_user_id=user_id(x_user_id), limit=max(1, min(limit, 200))
        )

    @app.post(
        "/memory/proposals/{proposal_id}/confirm",
        response_model=Memory,
        status_code=status.HTTP_201_CREATED,
    )
    def confirm_memory_proposal(
        proposal_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Memory:
        try:
            written = ctx.memory_store().confirm_proposal(
                owner_user_id=user_id(x_user_id), proposal_id=proposal_id
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if written is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return written

    @app.delete(
        "/memory/proposals/{proposal_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def dismiss_memory_proposal(
        proposal_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        dismissed = ctx.memory_store().dismiss_proposal(
            owner_user_id=user_id(x_user_id), proposal_id=proposal_id
        )
        if not dismissed:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/memory", response_model=list[Memory])
    def list_memory(
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
        scope: MemoryScope | None = None,
        thread_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        return ctx.memory_store().list_memories(
            owner_user_id=user_id(x_user_id),
            scope=scope,
            thread_id=thread_id,
            project_id=project_id,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )

    @app.post("/memory", response_model=Memory, status_code=status.HTTP_201_CREATED)
    def create_memory(
        request: CreateMemoryRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Memory:
        owner_user_id = user_id(x_user_id)
        store = ctx.memory_store()
        if request.scope is MemoryScope.THREAD:
            if request.thread_id is None:
                raise HTTPException(
                    status_code=422, detail="Thread scope requires a thread_id"
                )
            ctx.owned_thread(owner_user_id, request.thread_id)
        if request.scope is MemoryScope.PROJECT:
            if request.project_id is None:
                raise HTTPException(
                    status_code=422, detail="Project scope requires a project_id"
                )
            ctx.owned_project(owner_user_id, request.project_id)
        try:
            return store.add_memory(
                owner_user_id=owner_user_id,
                scope=request.scope,
                type=request.type,
                key=request.key,
                value=request.value,
                # The only path that creates a memory the user typed themselves.
                source=MemorySource.USER_EXPLICIT,
                thread_id=request.thread_id,
                project_id=request.project_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.patch("/memory/{memory_id}", response_model=Memory)
    def update_memory(
        memory_id: str,
        request: UpdateMemoryRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Memory:
        try:
            updated = ctx.memory_store().update_memory(
                owner_user_id=user_id(x_user_id),
                memory_id=memory_id,
                value=request.value,
                promote_to_user=request.promote_to_user,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if updated is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return updated

    @app.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_memory(
        memory_id: str,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> Response:
        deleted = ctx.memory_store().delete_memory(
            owner_user_id=user_id(x_user_id), memory_id=memory_id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

