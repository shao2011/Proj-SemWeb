"""Reporting a message.

The dialog tells the user the whole conversation is sent, so it is: the chat
is snapshotted server-side at the moment of the report and stored with it,
rather than referenced. That is what makes the report survive the reported
chat being deleted, and what keeps the promise in the dialog literally true.

Deliberately open to anonymous visitors. The person most likely to hit
something harmful during the free trial is someone who has not signed up, and
a report gate would simply mean the report never arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, status

from rag_ingestion.api.deps import ApiContext, user_id
from rag_ingestion.api.schemas import CreateReportRequest, ReportView

#: Cap on the snapshot, same ceiling the share view uses.
REPORT_MAX_MESSAGES = 500


def register(app: FastAPI, ctx: ApiContext) -> None:

    @app.post("/reports", response_model=ReportView, status_code=status.HTTP_201_CREATED)
    def create_report(
        request: CreateReportRequest,
        x_user_id: Annotated[str, Header(alias="X-User-ID", min_length=1)],
    ) -> ReportView:
        owner_user_id = user_id(x_user_id)
        # 404 rather than 403 for someone else's chat: whether a thread id
        # exists is not something a stranger gets to learn from a report form.
        ctx.owned_thread(owner_user_id, request.thread_id)
        store = ctx.thread_store()
        messages = (
            store.list_messages(
                owner_user_id=owner_user_id,
                thread_id=request.thread_id,
                limit=REPORT_MAX_MESSAGES,
            )
            or []
        )
        report_id = store.create_report(
            owner_user_id=owner_user_id,
            thread_id=request.thread_id,
            message_position=request.message_position,
            reason=request.reason,
            details=request.details,
            conversation=[
                {"role": message.role.value, "content": message.content}
                for message in messages
            ],
        )
        return ReportView(report_id=report_id, created_at=datetime.now(UTC))
