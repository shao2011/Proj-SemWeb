"""project icon and color

Revision ID: e4f7a2b9c8d1
Revises: b7e1c4d9a2f5
Create Date: 2026-08-13 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'e4f7a2b9c8d1'
down_revision: Union[str, Sequence[str], None] = 'b7e1c4d9a2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('projects', sa.Column('icon', sa.String(length=32), nullable=True))
    op.add_column('projects', sa.Column('color', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('projects', 'color')
    op.drop_column('projects', 'icon')
