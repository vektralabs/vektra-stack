"""The overdue-erasure alert marker (OQ-033: the LMS cannot fail its own privacy request).

``assess_erasure_records.overdue_alerted_at``: set, in the transaction that queues the
``erasure_overdue`` operator alert, on the request it reports. The worker's sweep selects the
open requests past ``VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S`` that have no marker, so a request is
reported once whatever happens to the job rows (the job retention may prune them), and a batch
never stalls on requests already reported. The partial index serves exactly that sweep.

No grant or policy changes, after ``a0002`` (RLS for the API role): the grants are table-level,
the worker already holds ``UPDATE`` on the table and writes the column (the sweep, the failure
record, the purge's abandoned requests), and the API, which holds ``INSERT`` and ``SELECT`` under
``a0002``'s site-bound policy, never writes it. The table stays under that policy; the worker
passes ``assess_unbound_roles``.
"""

from __future__ import annotations

from alembic import op

revision = "a0003"
down_revision = "a0002"
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
