"""thread recency sequence and one live memory row per key

Revision ID: 9f3c1d2b7a04
Revises: 801f5573326a
Create Date: 2026-08-08 21:10:00.000000

Two invariants the stores could not hold on their own.

`threads.sequence` was assigned as `max(sequence) + 1`, which is not atomic
across transactions: two threads touched at the same moment read the same
maximum and wrote it, and the recents order between them came down to the
execution plan. A sequence hands out each number once.

`memory` had no constraint saying one live row per key per scope. The store
superseded on the same key, but the read-decide-insert was not serialized, so a
lost supersede left two contradictory values live and whichever the query
returned first won.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 - autogenerate emits sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '9f3c1d2b7a04'
down_revision: Union[str, Sequence[str], None] = '801f5573326a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.text("CREATE SEQUENCE threads_recency_seq"))
    # Started above whatever the old counter reached, so the first thread touched
    # after this migration still sorts above every thread that came before it.
    op.execute(
        sa.text(
            "SELECT setval('threads_recency_seq', "
            "GREATEST((SELECT COALESCE(MAX(sequence), 0) FROM threads), 1))"
        )
    )

    # Supersede the duplicates any pre-constraint write left behind, oldest
    # losing to newest, which is the rule the store applies. Without this the
    # index below cannot be built on a database that already has a pair.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    memory_id,
                    FIRST_VALUE(memory_id) OVER (
                        PARTITION BY
                            owner_user_id,
                            scope,
                            COALESCE(thread_id, ''),
                            COALESCE(project_id, ''),
                            key
                        ORDER BY created_at DESC, memory_id DESC
                    ) AS winner
                FROM memory
                WHERE deleted_at IS NULL
            )
            UPDATE memory
            SET superseded_by = ranked.winner,
                deleted_at = now(),
                updated_at = now()
            FROM ranked
            WHERE memory.memory_id = ranked.memory_id
              AND ranked.memory_id <> ranked.winner
            """
        )
    )
    op.create_index(
        "memory_live_key_idx",
        "memory",
        [
            "owner_user_id",
            "scope",
            sa.text("coalesce(thread_id, '')"),
            sa.text("coalesce(project_id, '')"),
            "key",
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("memory_live_key_idx", table_name="memory")
    op.execute(sa.text("DROP SEQUENCE threads_recency_seq"))
