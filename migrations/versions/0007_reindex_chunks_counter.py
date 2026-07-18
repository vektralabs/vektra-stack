"""Add chunks_reindexed counter to reindex_jobs.

A reindex job recorded how many documents it walked, but not how many chunks it
actually re-embedded and wrote. In Qdrant mode it walked every document, wrote
nothing, and reported "completed" (BUG-023). The counter makes the work a job
did observable, and lets a job that stored nothing be told apart from a real one.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reindex_jobs",
        sa.Column(
            "chunks_reindexed",
            sa.INTEGER(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("reindex_jobs", "chunks_reindexed")
