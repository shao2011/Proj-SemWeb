"""Account routes: claiming an anonymous identity after the first login.

Until login, the frontend identifies the visitor with a random UUID. On the
first verified login that UUID's whole footprint — projects, threads,
memories, settings, usage totals, and the Qdrant tenant payloads — moves to
the Firebase uid, which is what makes "log in to keep your history" true.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import Field

from rag_ingestion.indexing.qdrant_index import QdrantHybridIndex
from rag_ingestion.models import StrictModel
from rag_ingestion.api.auth import authenticated_user_id, is_anonymous_id
from rag_ingestion.api.deps import ApiContext
from rag_ingestion.logger import get_logger

_log = get_logger(__name__)


class ClaimRequest(StrictModel):
    anonymous_user_id: str = Field(min_length=1, max_length=255)


class ClaimSummary(StrictModel):
    projects: int
    threads: int
    memories: int


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.post("/auth/claim", response_model=ClaimSummary)
    def claim_account(request: ClaimRequest) -> ClaimSummary:
        # Deliberately not the X-User-ID fallback: an unverified caller could
        # otherwise hand anyone's anonymous history to themselves.
        verified_user_id = authenticated_user_id()
        if verified_user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Claiming an account requires a verified login",
            )

        anonymous_user_id = request.anonymous_user_id.strip()
        if not anonymous_user_id or anonymous_user_id == verified_user_id:
            return ClaimSummary(projects=0, threads=0, memories=0)

        # Two server-side guards, so no client assertion is ever trusted:
        # the target must be anonymous-shaped, and it must never have logged
        # in itself — otherwise a valid login could absorb another account.
        if not is_anonymous_id(anonymous_user_id):
            raise HTTPException(
                status_code=403,
                detail="Only anonymous identities can be claimed",
            )
        if ctx.users is not None and ctx.users.is_registered(anonymous_user_id):
            raise HTTPException(
                status_code=403,
                detail="This identity belongs to an existing account",
            )

        projects = ctx.projects.reassign_owner(
            anonymous_user_id=anonymous_user_id,
            verified_user_id=verified_user_id,
        )
        threads = (
            ctx.threads.reassign_owner(
                anonymous_user_id=anonymous_user_id,
                verified_user_id=verified_user_id,
            )
            if ctx.threads is not None
            else 0
        )
        memories = (
            ctx.memory.reassign_owner(
                anonymous_user_id=anonymous_user_id,
                verified_user_id=verified_user_id,
            )
            if ctx.memory is not None
            else 0
        )
        for provider in ctx.registry.providers:
            bundle = ctx.registry.get(provider)
            QdrantHybridIndex(
                ctx.qdrant, bundle.collection_name, bundle.profile
            ).reassign_owner(
                anonymous_user_id=anonymous_user_id,
                verified_user_id=verified_user_id,
            )

        _log.info(
            "Claimed anonymous account %s into %s: projects=%d threads=%d memories=%d",
            anonymous_user_id,
            verified_user_id,
            projects,
            threads,
            memories,
        )
        return ClaimSummary(projects=projects, threads=threads, memories=memories)
