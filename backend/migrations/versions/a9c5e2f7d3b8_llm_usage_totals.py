"""llm usage totals

Revision ID: a9c5e2f7d3b8
Revises: e4f7a2b9c8d1
Create Date: 2026-08-13 23:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'a9c5e2f7d3b8'
down_revision: Union[str, Sequence[str], None] = 'e4f7a2b9c8d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'llm_usage',
        sa.Column('owner_user_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
        sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
        sa.Column('total_tokens', sa.BigInteger(), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('owner_user_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('llm_usage')
