"""Backfill ``assess_reset_grants.consumed_at`` (REQ-087, the subject export of #215).

Until this release no code wrote ``consumed_at``: Start eligibility counts grants and was right,
but ``GET /privacy/subjects/{userid}/activities`` exported every reset as unconsumed. The
pre-flight now consumes the grant at commit (``worker.preflight._CONSUME_GRANT``): the grant
written after session N (``session_no = N``) takes the ``committed_at`` of session N+1 of the
same (site, cmid, student). This revision applies the same rule, once, to the rows written
before it: a grant whose next session committed gets that session's ``committed_at``; a grant
whose next session never committed (none started, or only rolled-back Starts, whose rows are
deleted) stays NULL. Rows already set are left alone.

Data only: no table, column, index, grant or policy changes, so the schema and privilege
goldens are unchanged. The tables are owned by the migration user and not ``FORCE``d under RLS
(``a0002``), so the update sees every site's rows.

The downgrade leaves the data as it is: from a0004 on, ``consumed_at`` existed and was allowed
to be set (the worker held ``UPDATE (consumed_at)`` already), and the backfilled values cannot
be told apart from the ones the pre-flight writes after this release.
"""

from __future__ import annotations

from alembic import op

revision = "a0005"
down_revision = "a0004"
branch_labels = None
depends_on = None

#: the pre-flight's rule (``_CONSUME_GRANT``) over every committed session at once
BACKFILL = (
    "UPDATE public.assess_reset_grants g SET consumed_at = s.committed_at "
    "FROM public.assess_sessions s WHERE g.site_id = s.site_id AND g.cmid = s.cmid "
    "AND g.student_userid = s.student_userid AND s.session_no = g.session_no + 1 "
    "AND s.committed_at IS NOT NULL AND g.consumed_at IS NULL"
)


def upgrade() -> None:
    op.execute(BACKFILL)


def downgrade() -> None:
    pass  # data only, see the module docstring
