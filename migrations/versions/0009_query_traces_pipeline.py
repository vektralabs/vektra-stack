"""Add ``query_traces.pipeline`` to the platform.

The column names the emitter of a trace: NULL for every ``/api/v1/query`` (RAG) trace, a
pipeline name for any other emitter. Analytics maps it (``QueryTraceOrm.pipeline``) and every
read filters on it (``pipeline IS NULL``: RAG metrics never absorb another emitter's traces), so
the column belongs to the platform branch: a deployment that migrates only ``platform@head``
must be able to store and read traces.

It was created by the assessment schema before (ADR-0033 D7 moved that schema to its own branch,
applied only where the feature is enabled); the assessment branch now depends on this revision.
Same definition, same position (the table's last column).

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE query_traces ADD COLUMN pipeline VARCHAR(32) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE query_traces DROP COLUMN pipeline")
