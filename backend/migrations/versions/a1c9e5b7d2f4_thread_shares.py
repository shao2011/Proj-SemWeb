"""thread share links

Revision ID: a1c9e5b7d2f4
Revises: d4b8f1c3a7e2
Create Date: 2026-08-16 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'a1c9e5b7d2f4'
down_revision: Union[str, Sequence[str], None] = 'd4b8f1c3a7e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Guarded like the users registry: a dev database that ran `create_all`
    already has the table, and stamping the revision over it is correct.
    """
    if sa.inspect(op.get_bind()).has_table('thread_shares'):
        return
    op.create_table(
        'thread_shares',
        sa.Column('share_id', sa.String(length=64), primary_key=True),
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('owner_user_id', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('shared_through_position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['thread_id'], ['threads.thread_id'], ondelete='CASCADE'
        ),
        sa.UniqueConstraint('thread_id', name='thread_shares_thread_key'),
    )
    op.create_index(
        'ix_thread_shares_owner_user_id', 'thread_shares', ['owner_user_id']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_thread_shares_owner_user_id', table_name='thread_shares')
    op.drop_table('thread_shares')
