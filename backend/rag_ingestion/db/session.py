from __future__ import annotations

from sqlalchemy import Engine
from sqlmodel import Session, create_engine


def build_engine(database_url: str, **overrides: object) -> Engine:
    """Create an engine with pool settings suited to a single API container.

    `pool_pre_ping` survives idle disconnects and `pool_recycle` keeps
    connections under typical server-side idle timeouts.
    """
    options: dict[str, object] = {
        "echo": False,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    options.update(overrides)
    return create_engine(database_url, **options)  # type: ignore[arg-type]


def session_factory(engine: Engine):
    def open_session() -> Session:
        return Session(engine)

    return open_session
