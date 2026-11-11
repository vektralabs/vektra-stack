"""Assess discussion traces in ``query_traces`` (plan assess-llm-runner, task C).

The discussion runner emits a content-free QueryTrace per turn and per Part B. Two additions make
those rows distinguishable from RAG query traces:

- ``query_traces.pipeline``: nullable, ``NULL`` for every existing and future ``/api/v1/query``
  trace, ``'assess-discussion'`` for assess. Analytics aggregations filter ``pipeline IS NULL``,
  so RAG metrics never absorb assess turns.
- a reserved ``namespaces`` row ``assess-discussion``: ``namespace_id`` is NOT NULL with a FK, and
  assess traces need a namespace that holds no RAG content (the ingest API refuses that id).

The upgrade **refuses** when a namespace with that id already exists. Seeding with
``ON CONFLICT DO NOTHING`` would keep a deployment's own ``assess-discussion`` namespace and turn
it read-only for ever (ingest, config and delete all refuse a reserved id), and the downgrade
would then delete it. Refusing first is what makes the downgrade safe: the row it deletes is
always the one this upgrade created (it also checks the ``reserved`` marker).

Downgrade deletes the assess traces first (they would otherwise block the namespace delete and
lose their marker), then the namespace and the column.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

ASSESS_NAMESPACE = "assess-discussion"


PRE_EXISTING_MESSAGE = (
    f"Revision 0019 reserves the namespace id '{ASSESS_NAMESPACE}', and this database already "
    "has a namespace with that id. It was not changed, and the upgrade was rolled back: the "
    "database keeps the revision it had before this run. "
    "Keeping it would make it read-only for ever: a reserved namespace refuses ingest, "
    "configuration and deletion. Way out, on the release you run now: re-ingest its documents "
    "into a namespace with another name, delete its documents, then delete the row "
    f"(DELETE FROM namespaces WHERE id = '{ASSESS_NAMESPACE}'; it fails while any row still "
    "references it), and start the upgrade again."
)


def upgrade() -> None:
    existing = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM namespaces WHERE id = :id"), {"id": ASSESS_NAMESPACE}
        )
        .first()
    )
    if existing is not None:
        raise RuntimeError(PRE_EXISTING_MESSAGE)
    op.execute("ALTER TABLE query_traces ADD COLUMN pipeline VARCHAR(32) NULL")
    op.execute(
        f"""
        INSERT INTO namespaces (id, display_name, config)
        VALUES ('{ASSESS_NAMESPACE}', 'Assess discussion traces (reserved)',
                '{{"reserved": true}}'::jsonb)
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM query_traces WHERE namespace_id = '{ASSESS_NAMESPACE}'")
    # the upgrade refuses a pre-existing row, so this is the row it created; the marker check is
    # a second lock, never the first
    op.execute(
        f"DELETE FROM namespaces WHERE id = '{ASSESS_NAMESPACE}' "
        "AND config->>'reserved' = 'true'"
    )
    op.execute("ALTER TABLE query_traces DROP COLUMN pipeline")
