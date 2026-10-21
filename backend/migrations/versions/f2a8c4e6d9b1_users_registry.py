"""users registry for verified identities

Revision ID: f2a8c4e6d9b1
Revises: c8d2f5a1b6e3
Create Date: 2026-08-15 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'f2a8c4e6d9b1'
down_revision: Union[str, Sequence[str], None] = 'c8d2f5a1b6e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guarded: dev databases that ran the test tooling may already have the
    table; stamping the revision over it is correct, recreating it is not.
    """
    if sa.inspect(op.get_bind()).has_table('users'):
        return
    op.create_table(
        'users',
        sa.Column('uid', sa.String(length=255), primary_key=True),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('display_name', sa.String(length=200), nullable=True),
        sa.Column('first_login_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('users')
