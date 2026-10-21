"""memory audit sequence

Revision ID: cb5a1dde9f49
Revises: 82a8b357280a
Create Date: 2026-08-05 11:42:56.190967

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'cb5a1dde9f49'
down_revision: Union[str, Sequence[str], None] = '82a8b357280a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the audit trail's ordering key.

    `82a8b357280a` shipped `memory_audit` without one, leaving the trail ordered
    by `created_at`; the clock resolution on some hosts puts an ADD and the
    UPDATE that follows it on one identical value, so their order was arbitrary.

    Added with a server default so the statement also backfills any rows the
    previous revision already accepted, then dropped, because the sequence is
    assigned by the store rather than by the database.
    """
    op.add_column(
        "memory_audit",
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("memory_audit", "sequence", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("memory_audit", "sequence")
