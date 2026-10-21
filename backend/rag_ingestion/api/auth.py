from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from rag_ingestion.storage.threads import normalize_email

#: The one shape an anonymous identity may take. Enforced on the server, so
#: the split between anonymous and verified identities holds by construction:
#: the X-User-ID fallback accepts nothing else, and a login token whose uid
#: matches it is rejected outright.
ANON_ID_RE = re.compile(r"^anon_[0-9a-f]{32}$")


def is_anonymous_id(value: str) -> bool:
    return ANON_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class VerifiedIdentity:
    """What a valid login token proves: who, plus the profile it carries.

    `email_verified` is carried separately from `email` because only the
    verified pair means anything for access control: an address the provider
    has not proved is one anybody can claim by signing up with it.
    """

    uid: str
    email: str | None = None
    display_name: str | None = None
    email_verified: bool = False

_current_user_id: ContextVar[str | None] = ContextVar(
    "api_current_user_id", default=None
)
#: Set only when the token carries a *verified* address, so a value here is
#: proof of the address rather than a claim about it.
_current_user_email: ContextVar[str | None] = ContextVar(
    "api_current_user_email", default=None
)


def authenticated_user_id() -> str | None:
    return _current_user_id.get()


def set_authenticated_user_id(value: str | None) -> Token[str | None]:
    return _current_user_id.set(value)


def reset_authenticated_user_id(token: Token[str | None]) -> None:
    _current_user_id.reset(token)


def authenticated_email() -> str | None:
    """The caller's proven address, normalized, or None.

    None covers every case that must not match an invitation: no token, no
    address on the token, and an address the provider never verified.
    """
    return _current_user_email.get()


def set_authenticated_email(value: str | None) -> Token[str | None]:
    return _current_user_email.set(normalize_email(value) if value else None)


def reset_authenticated_email(token: Token[str | None]) -> None:
    _current_user_email.reset(token)


class FirebaseTokenError(ValueError):
    pass


@dataclass(frozen=True)
class FirebaseTokenVerifier:
    app: firebase_admin.App
    project_id: str | None = None

    @classmethod
    def from_service_account(
        cls,
        service_account_path: str,
        *,
        project_id: str | None = None,
    ) -> FirebaseTokenVerifier:
        path = Path(service_account_path).expanduser()
        if not path.is_file():
            raise ValueError(f"Service account file does not exist: {path}")
        name = f"rag-firebase-{abs(hash(str(path.resolve())))}"
        try:
            app = firebase_admin.get_app(name)
        except ValueError:
            app = firebase_admin.initialize_app(
                credentials.Certificate(str(path)),
                name=name,
            )
        return cls(app=app, project_id=project_id)

    def verify_bearer(self, authorization: str) -> VerifiedIdentity:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token.strip():
            raise FirebaseTokenError("Invalid Authorization header")
        return self.verify_token(token.strip())

    def verify_token(self, token: str) -> VerifiedIdentity:
        try:
            decoded = firebase_auth.verify_id_token(token, app=self.app)
        except Exception as error:  # pragma: no cover - provider errors vary
            raise FirebaseTokenError("Invalid Firebase ID token") from error

        uid = str(decoded.get("uid", "")).strip()
        if not uid:
            raise FirebaseTokenError("Firebase token has no uid")

        if self.project_id is not None:
            aud = str(decoded.get("aud", "")).strip()
            if aud != self.project_id:
                raise FirebaseTokenError("Firebase token project mismatch")

        email = decoded.get("email")
        name = decoded.get("name")
        return VerifiedIdentity(
            uid=uid,
            email=str(email) if email else None,
            display_name=str(name) if name else None,
            email_verified=bool(decoded.get("email_verified")),
        )
