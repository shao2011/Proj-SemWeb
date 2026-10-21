"""admin accounts, audit trail, disabled users, report resolution

Revision ID: f2b8d4a6c1e9
Revises: d5b1c8e3a9f7
Create Date: 2026-08-18 23:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'f2b8d4a6c1e9'
down_revision: Union[str, Sequence[str], None] = 'd5b1c8e3a9f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nothing is backfilled. Every existing user is active (`disabled_at` NULL)
    and every existing report is unresolved, which is exactly what both meant
    before these columns existed. Bootstrap admins live in `ADMIN_EMAILS`, not
    in `admin_users`, so the table starts empty on purpose.
    """
    inspector = sa.inspect(op.get_bind())

    user_columns = {column['name'] for column in inspector.get_columns('users')}
    if 'disabled_at' not in user_columns:
        op.add_column(
            'users',
            sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
        )

    report_columns = {
        column['name'] for column in inspector.get_columns('message_reports')
    }
    if 'resolved_at' not in report_columns:
        op.add_column(
            'message_reports',
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        )
    if 'resolved_by' not in report_columns:
        op.add_column(
            'message_reports',
            sa.Column('resolved_by', sa.String(length=320), nullable=True),
        )

    if not inspector.has_table('admin_users'):
        op.create_table(
            'admin_users',
            sa.Column('email', sa.String(length=320), primary_key=True),
            sa.Column('granted_by', sa.String(length=320), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        )

    if not inspector.has_table('admin_audit'):
        op.create_table(
            'admin_audit',
            sa.Column('audit_id', sa.String(length=64), primary_key=True),
            sa.Column('actor_email', sa.String(length=320), nullable=False),
            sa.Column('action', sa.String(length=32), nullable=False),
            sa.Column('target', sa.String(length=320), nullable=False),
            sa.Column('detail', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index('ix_admin_audit_actor_email', 'admin_audit', ['actor_email'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_admin_audit_actor_email', table_name='admin_audit')
    op.drop_table('admin_audit')
    op.drop_table('admin_users')
    op.drop_column('message_reports', 'resolved_by')
    op.drop_column('message_reports', 'resolved_at')
    op.drop_column('users', 'disabled_at')
