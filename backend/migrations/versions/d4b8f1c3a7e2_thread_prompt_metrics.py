"""thread prompt metrics: measured size of the last prompt, per thread

Revision ID: d4b8f1c3a7e2
Revises: f2a8c4e6d9b1
Create Date: 2026-08-15 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'd4b8f1c3a7e2'
down_revision: Union[str, Sequence[str], None] = 'f2a8c4e6d9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'threads', sa.Column('last_prompt_tokens', sa.Integer(), nullable=True)
    )
    op.add_column(
        'threads', sa.Column('last_prompt_chars', sa.Integer(), nullable=True)
    )
    op.add_column(
        'threads', sa.Column('last_model_id', sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('threads', 'last_model_id')
    op.drop_column('threads', 'last_prompt_chars')
    op.drop_column('threads', 'last_prompt_tokens')
