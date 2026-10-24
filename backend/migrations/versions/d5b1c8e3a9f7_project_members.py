"""project members with roles

Revision ID: d5b1c8e3a9f7
Revises: c9e2a4b8d6f3
Create Date: 2026-08-17 21:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'd5b1c8e3a9f7'
down_revision: Union[str, Sequence[str], None] = 'c9e2a4b8d6f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nothing is backfilled: an existing project has no members, which is
    exactly what it meant before this table existed. The owner stays on
    `projects.owner_user_id` and is deliberately not written here, so the two
    can never disagree about who owns a project.
    """
    if sa.inspect(op.get_bind()).has_table('project_members'):
        return
    op.create_table(
        'project_members',
        sa.Column('member_id', sa.String(length=64), primary_key=True),
        sa.Column('project_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'], ['projects.project_id'], ondelete='CASCADE'
        ),
        sa.UniqueConstraint('project_id', 'email', name='project_members_key'),
    )
    op.create_index('ix_project_members_email', 'project_members', ['email'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_project_members_email', table_name='project_members')
    op.drop_table('project_members')
