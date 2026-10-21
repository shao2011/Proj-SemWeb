"""Metadata entry point for Alembic.

Every table module must be imported here so `SQLModel.metadata` is fully
populated before autogenerate runs, otherwise migrations silently miss tables.
"""

from sqlmodel import SQLModel

from rag_ingestion.db import tables  # noqa: F401

target_metadata = SQLModel.metadata
