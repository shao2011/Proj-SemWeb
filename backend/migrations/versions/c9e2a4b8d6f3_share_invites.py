"""share access mode and email recipients

Revision ID: c9e2a4b8d6f3
Revises: b3d7f1e9c5a2
Create Date: 2026-08-16 03:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'c9e2a4b8d6f3'
down_revision: Union[str, Sequence[str], None] = 'b3d7f1e9c5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    `access` defaults to 'link', which is exactly what every existing share
    already is: a public snapshot. Backfilling anything else would silently
    change who can open a link that is already out in the world.
    """
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('thread_shares')}
    if 'access' not in columns:
        op.add_column(
            'thread_shares',
            sa.Column(
                'access',
                sa.String(length=16),
                nullable=False,
                server_default='link',
            ),
        )
    if not inspector.has_table('thread_share_recipients'):
        op.create_table(
            'thread_share_recipients',
            sa.Column('recipient_id', sa.String(length=64), primary_key=True),
            sa.Column('share_id', sa.String(length=64), nullable=False),
            sa.Column('email', sa.String(length=320), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ['share_id'], ['thread_shares.share_id'], ondelete='CASCADE'
            ),
            sa.UniqueConstraint(
                'share_id', 'email', name='thread_share_recipients_key'
            ),
        )
        op.create_index(
            'ix_thread_share_recipients_email', 'thread_share_recipients', ['email']
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_thread_share_recipients_email', table_name='thread_share_recipients'
    )
    op.drop_table('thread_share_recipients')
    op.drop_column('thread_shares', 'access')
