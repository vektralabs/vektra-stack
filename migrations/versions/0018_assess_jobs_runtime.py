"""Job-loop runtime additions to ``assess_jobs`` (plan assess-job-loop, group A).

Revision 0013 already created the outbox table the worker claims from (``state`` in
ready/leased/done/failed/dead, ``args`` ids-only, ``dedupe_key``, ``leased_by``,
``max_attempts``). The runtime needs three more things:

- ``last_error_code``: a content-free code for the last failed attempt (never a message);
- an index that serves the claim query by kind, for both fresh ``ready`` rows and ``leased``
  rows whose lease expired (the 0013 index only covers ``ready``);
- storage tuning for a table that is updated on every claim, complete and release:
  ``fillfactor`` leaves room for HOT updates and autovacuum keeps up with the churn.

No grant changes: the worker already has UPDATE on ``assess_jobs`` (revision 0017).

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE assess_jobs ADD COLUMN last_error_code TEXT NULL")
    op.execute("""
        CREATE INDEX ix_assess_jobs_kind_claim
            ON assess_jobs (kind, not_before) WHERE state IN ('ready', 'leased')
    """)
    op.execute("DROP INDEX ix_assess_jobs_claim")
    op.execute("""
        ALTER TABLE assess_jobs SET (
            fillfactor = 70,
            autovacuum_vacuum_scale_factor = 0.01,
            autovacuum_vacuum_threshold = 200,
            autovacuum_analyze_scale_factor = 0.02
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE assess_jobs RESET (
            fillfactor, autovacuum_vacuum_threshold, autovacuum_analyze_scale_factor
        )
    """)
    op.execute("ALTER TABLE assess_jobs SET (autovacuum_vacuum_scale_factor = 0.02)")
    op.execute("""
        CREATE INDEX ix_assess_jobs_claim
            ON assess_jobs (state, not_before) WHERE state = 'ready'
    """)
    op.execute("DROP INDEX ix_assess_jobs_kind_claim")
    op.execute("ALTER TABLE assess_jobs DROP COLUMN last_error_code")
