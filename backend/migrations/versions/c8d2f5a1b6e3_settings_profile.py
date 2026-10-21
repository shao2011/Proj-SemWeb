"""user settings profile: display name and email from login

Revision ID: c8d2f5a1b6e3
Revises: a9c5e2f7d3b8
Create Date: 2026-08-15 07:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'c8d2f5a1b6e3'
down_revision: Union[str, Sequence[str], None] = 'a9c5e2f7d3b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'user_settings', sa.Column('display_name', sa.String(length=200), nullable=True)
    )
    op.add_column(
        'user_settings', sa.Column('email', sa.String(length=320), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_settings', 'email')
    op.drop_column('user_settings', 'display_name')
