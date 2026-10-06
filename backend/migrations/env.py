"""Alembic environment.

The URL comes from RuntimeConfig, not alembic.ini, so migrations and the app
read the same `.env` under the same precedence rules.
"""

from logging.config import fileConfig

from alembic import context

from rag_ingestion.config import RuntimeConfig
from rag_ingestion.db.base import target_metadata
from rag_ingestion.db.session import build_engine

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which silences every logger
    # already configured, including the application's own. Harmless when Alembic
    # runs as its own process, not harmless when it is driven in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = RuntimeConfig.from_environment().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller may hand us its own connection through `config.attributes`, which
    # is how the fresh-database test drives the chain into a throwaway schema:
    # the URL below is one database for the whole process, so it cannot target a
    # per-test `search_path`.
    injected = context.config.attributes.get("connection")
    if injected is not None:
        _run(injected)
        return
    connectable = build_engine(database_url, poolclass=None)
    with connectable.connect() as connection:
        _run(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
