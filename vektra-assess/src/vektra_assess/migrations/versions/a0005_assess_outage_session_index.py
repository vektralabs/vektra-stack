"""An index on ``assess_outage_incidents(session_id)``.

The discussion stream's status read (``api/events.py`` ``_STATUS``) counts a live session's
outage intervals on every poll (one per second per open connection) and reads the open one's
reason and start in OUTAGE; the session snapshot, the transcript and the probe read them by
session too. The table had only its primary key, so each of those reads scanned it whole (the
foreign key to ``assess_sessions`` has no index of its own either, which the ``ON DELETE
CASCADE`` of a session's deletion also pays).

An index only: no privilege changes, so no grant matrix here (``a0004`` still carries it).
"""

from __future__ import annotations

from alembic import op

revision = "a0005"
down_revision = "a0004"
branch_labels = None
depends_on = None

INDEX = "ix_assess_outage_incidents_session"


def upgrade() -> None:
    op.execute(f"CREATE INDEX {INDEX} ON public.assess_outage_incidents (session_id)")


def downgrade() -> None:
    op.execute(f"DROP INDEX public.{INDEX}")
