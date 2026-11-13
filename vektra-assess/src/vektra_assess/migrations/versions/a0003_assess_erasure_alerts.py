"""The overdue-erasure alert marker (OQ-033: the LMS cannot fail its own privacy request).

``assess_erasure_records.overdue_alerted_at``: set, in the transaction that queues the
``erasure_overdue`` operator alert, on the request it reports. The worker's sweep selects the
open requests past ``VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S`` that have no marker, so a request is
reported once whatever happens to the job rows (the job retention may prune them), and a batch
never stalls on requests already reported. The partial index serves exactly that sweep.

No grant changes: the worker already holds ``UPDATE`` on the table (the API holds ``INSERT`` and
``SELECT`` and never writes the column). Revision id ``a0003`` (``a0002`` is taken by the RLS
revision of a parallel branch); it chains after the assess head of its base branch and is re-
pointed after ``a0002`` if that merges first.
"""

from __future__ import annotations

from alembic import op

revision = "a0003"
down_revision = "a0001"
branch_labels = None
depends_on = None

INDEX = "ix_assess_erasure_records_overdue_open"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.assess_erasure_records "
        "ADD COLUMN overdue_alerted_at timestamp with time zone"
    )
    op.execute(
        f"CREATE INDEX {INDEX} ON public.assess_erasure_records (requested_at, id) "
        "WHERE completed_at IS NULL AND overdue_alerted_at IS NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX public.{INDEX}")
    op.execute(
        "ALTER TABLE public.assess_erasure_records DROP COLUMN overdue_alerted_at"
    )
