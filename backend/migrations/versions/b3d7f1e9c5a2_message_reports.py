"""message reports

Revision ID: b3d7f1e9c5a2
Revises: a1c9e5b7d2f4
Create Date: 2026-08-16 02:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'b3d7f1e9c5a2'
down_revision: Union[str, Sequence[str], None] = 'a1c9e5b7d2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    No foreign key on `thread_id` on purpose: a report must outlive the chat
    it is about, so the conversation is stored inline rather than referenced.
    """
    if sa.inspect(op.get_bind()).has_table('message_reports'):
        return
    op.create_table(
        'message_reports',
        sa.Column('report_id', sa.String(length=64), primary_key=True),
        sa.Column('owner_user_id', sa.String(length=255), nullable=False),
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('message_position', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(length=32), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('conversation', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        'ix_message_reports_owner_user_id', 'message_reports', ['owner_user_id']
    )
    op.create_index('ix_message_reports_thread_id', 'message_reports', ['thread_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_message_reports_thread_id', table_name='message_reports')
    op.drop_index('ix_message_reports_owner_user_id', table_name='message_reports')
    op.drop_table('message_reports')
