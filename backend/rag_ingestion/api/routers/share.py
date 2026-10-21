"""Reading a shared chat, and taking it into a chat of your own.

Two access modes on one share. A `link` share is public: the token in the URL
is the whole credential, and the payload is scrubbed of everything that is not
the transcript. An `invite` share is readable only by the addresses the owner
named, matched against the *verified* email inside the caller's token -- never
against anything the client sends, and never against an address the provider
has not proved.

An invited reader never writes into the owner's thread. `POST /share/{token}/fork`
copies the snapshot into a thread they own, with no project attached, which is
what keeps the owner's documents out of reach: retrieval has nothing to run
against rather than a check that somebody has to remember.

`create_query_app`'s auth middleware treats this path prefix as optional-auth:
a missing token must not 401 here, but a token that is present is still
verified, because that is what decides access.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status

from rag_ingestion.api.auth import authenticated_email, authenticated_user_id
from rag_ingestion.api.deps import ApiContext
from rag_ingestion.api.schemas import SharedChatView, SharedMessage, ThreadView
from rag_ingestion.storage.threads import ShareAccess, ThreadShare

#: Ceiling on a shared transcript. A very long chat is reported as truncated
#: rather than quietly cut, so the page never presents part of a conversation
#: as the whole of it.
SHARE_MAX_MESSAGES = 500

#: The path the auth middleware treats as optional-auth. Kept here so the rule
#: and the routes cannot drift apart.
PUBLIC_PREFIX = "/share/"

#: Search engines index whatever they can reach, and a chat shared with three
#: people must not turn up in results. Repeated on the error paths because
#: FastAPI builds its own response for an HTTPException.
NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


def register(app: FastAPI, ctx: ApiContext) -> None:

    def readable(share: ThreadShare) -> None:
        """Raise unless the caller may open this share.

        A link share is open to everyone. An invite share needs a login, and
        then either the owner's own uid or a verified address on the list.
        """
        if share.access is ShareAccess.LINK:
            return
        if authenticated_user_id() is None:
            raise HTTPException(
                status_code=401,
                detail="login_required_for_share",
                headers=NOINDEX,
            )
        if authenticated_user_id() == share.owner_user_id:
            return
        email = authenticated_email()
        if email is None or email not in share.recipients:
            raise HTTPException(
                status_code=403,
                detail="not_invited_to_share",
                headers=NOINDEX,
            )

    def load(share_id: str) -> ThreadShare:
        share = ctx.thread_store().get_share(share_id=share_id)
        if share is None:
            # Same answer for a revoked link, a deleted chat, and a token that
            # never existed: which one it was is not a stranger's business.
            raise HTTPException(
                status_code=404,
                detail="This link is no longer valid",
                headers=NOINDEX,
            )
        return share

    @app.get("/share/{share_id}", response_model=SharedChatView)
    def read_shared_chat(share_id: str, response: Response) -> SharedChatView:
        response.headers.update(NOINDEX)
        share = load(share_id)
        readable(share)
        messages = (
            ctx.thread_store().list_messages_range(
                owner_user_id=share.owner_user_id,
                thread_id=share.thread_id,
                from_position=1,
                through_position=share.shared_through_position,
                limit=SHARE_MAX_MESSAGES,
            )
            or []
        )
        return SharedChatView(
            title=share.title,
            created_at=share.created_at,
            messages=[
                SharedMessage(
                    role=message.role,
                    content=message.content,
                    created_at=message.created_at,
                )
                for message in messages
            ],
            message_count=share.shared_through_position,
            truncated=len(messages) < share.shared_through_position,
            # Only the invite flow was asked to be continuable; a public link
            # stays the read-only snapshot it has always been.
            can_continue=share.access is ShareAccess.INVITE,
        )

    @app.post(
        "/share/{share_id}/fork",
        response_model=ThreadView,
        status_code=status.HTTP_201_CREATED,
    )
    def continue_shared_chat(share_id: str) -> ThreadView:
        share = load(share_id)
        if share.access is not ShareAccess.INVITE:
            raise HTTPException(status_code=403, detail="share_is_read_only")
        readable(share)
        # A copy has to belong to someone, and the anonymous namespace cannot
        # be on an invitation, so this is a login by construction.
        reader = authenticated_user_id()
        if reader is None:
            raise HTTPException(status_code=401, detail="login_required_for_share")
        forked = ctx.thread_store().fork_share(share=share, new_owner_user_id=reader)
        if forked is None:
            raise HTTPException(status_code=404, detail="This link is no longer valid")
        return ThreadView(
            **forked.model_dump(),
            context_used_percent=ctx.context_used_percent(forked),
        )
