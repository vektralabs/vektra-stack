"""Index of a job chain's root (plan assess-privacy-retention 12b, review #8).

The notification chains name their first job in ``args->>'root_job_id'``; the ``delivery_retry``
safety net (``delivery.notify.rescue_dead_chains``) looks a chain up by it on every tick. Without
an index that is a scan of ``assess_jobs`` per live notification row. The partial predicate is
implied by the equality the queries use, so the planner can take the index.

No privilege changes.

Revision ID: 0043
Revises: 0042
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_assess_jobs_root ON assess_jobs ((args->>'root_job_id')) "
        "WHERE (args->>'root_job_id') IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_assess_jobs_root")
