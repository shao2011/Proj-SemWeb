"""memory, proposals, audit, user settings

Revision ID: 82a8b357280a
Revises: 6b64a4f82cd9
Create Date: 2026-08-05 11:35:49.385458

`memory_audit` is deliberately created without its `sequence` column: the very
next revision adds it, and moving it here would leave that revision trying to
add a column that already exists. The pair is kept in its original order so a
database already at `cb5a1dde9f49` needs no repair.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '82a8b357280a'
down_revision: Union[str, Sequence[str], None] = '6b64a4f82cd9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'memory',
        sa.Column('memory_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('owner_user_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('scope', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('thread_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('project_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('type', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column('superseded_by', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['thread_id'], ['threads.thread_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('memory_id'),
    )
    op.create_index(op.f('ix_memory_owner_user_id'), 'memory', ['owner_user_id'], unique=False)
    op.create_index('memory_owner_scope_idx', 'memory', ['owner_user_id', 'scope', 'deleted_at'], unique=False)
    op.create_index('memory_owner_key_idx', 'memory', ['owner_user_id', 'key'], unique=False)

    op.create_table(
        'memory_proposal',
        sa.Column('proposal_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('owner_user_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('thread_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('suggested_scope', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('type', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source_message_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['thread_id'], ['threads.thread_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('proposal_id'),
    )
    op.create_index(op.f('ix_memory_proposal_owner_user_id'), 'memory_proposal', ['owner_user_id'], unique=False)

    op.create_table(
        'memory_audit',
        sa.Column('audit_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        # Not a foreign key: the audit trail must survive its subject.
        sa.Column('memory_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('op', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column('source_message_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('audit_id'),
    )
    op.create_index(op.f('ix_memory_audit_memory_id'), 'memory_audit', ['memory_id'], unique=False)

    op.create_table(
        'user_settings',
        sa.Column('owner_user_id', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('memory_enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('owner_user_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('user_settings')
    op.drop_index(op.f('ix_memory_audit_memory_id'), table_name='memory_audit')
    op.drop_table('memory_audit')
    op.drop_index(op.f('ix_memory_proposal_owner_user_id'), table_name='memory_proposal')
    op.drop_table('memory_proposal')
    op.drop_index('memory_owner_key_idx', table_name='memory')
    op.drop_index('memory_owner_scope_idx', table_name='memory')
    op.drop_index(op.f('ix_memory_owner_user_id'), table_name='memory')
    op.drop_table('memory')
