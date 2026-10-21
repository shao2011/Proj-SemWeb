"""The registry of verified identities.

Protocol only; the Postgres implementation lives in ``pg_users``. Kept
deliberately tiny: the auth middleware records logins, and account claiming
asks one question — "did this id ever log in?".
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class UserStore(Protocol):
    def record_login(
        self, *, uid: str, email: str | None, display_name: str | None
    ) -> None: ...

    def is_registered(self, uid: str) -> bool: ...

    def is_disabled(self, uid: str) -> bool:
        """Whether this account has been locked by an administrator.

        Checked by the auth middleware on every authenticated request, so a
        disabled account is refused everywhere rather than route by route.
        Unknown ids (anonymous visitors) are not disabled.
        """
        ...

    def find_uid_by_email(self, email: str) -> str | None:
        """Who signed in with this address, if anyone ever did.

        Ownership lives on a uid, but an owner hands a project over by naming
        an address, so a transfer has to resolve one to the other. Returns None
        for an address that has never logged in -- there is nobody to give it
        to yet.
        """
        ...

    def close(self) -> None: ...
