"""Assess discussion traces in ``query_traces`` (plan assess-llm-runner, task C).

The discussion runner emits a content-free QueryTrace per turn and per Part B. Two additions make
those rows distinguishable from RAG query traces:

- ``query_traces.pipeline``: nullable, ``NULL`` for every existing and future ``/api/v1/query``
  trace, ``'assess-discussion'`` for assess. Analytics aggregations filter ``pipeline IS NULL``,
  so RAG metrics never absorb assess turns.
- a reserved ``namespaces`` row ``assess-discussion``: ``namespace_id`` is NOT NULL with a FK, and
  assess traces need a namespace that holds no RAG content (the ingest API refuses that id).

Downgrade deletes the assess traces first (they would otherwise block the namespace delete and
lose their marker), then the namespace and the column.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

ASSESS_NAMESPACE = "assess-discussion"


def upgrade() -> None:
    op.execute("ALTER TABLE query_traces ADD COLUMN pipeline VARCHAR(32) NULL")
    op.execute(
        f"""
        INSERT INTO namespaces (id, display_name, config)
        VALUES ('{ASSESS_NAMESPACE}', 'Assess discussion traces (reserved)',
                '{{"reserved": true}}'::jsonb)
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM query_traces WHERE namespace_id = '{ASSESS_NAMESPACE}'")
    op.execute(f"DELETE FROM namespaces WHERE id = '{ASSESS_NAMESPACE}'")
    op.execute("ALTER TABLE query_traces DROP COLUMN pipeline")
