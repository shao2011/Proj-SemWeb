"""project file trash

Revision ID: b7e1c4d9a2f5
Revises: 9f3c1d2b7a04
Create Date: 2026-08-11 23:16:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'b7e1c4d9a2f5'
down_revision: Union[str, Sequence[str], None] = '9f3c1d2b7a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'project_files',
        sa.Column('trashed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('project_files', 'trashed_at')
