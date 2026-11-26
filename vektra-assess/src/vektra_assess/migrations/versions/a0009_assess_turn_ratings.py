"""Thumbs up / thumbs down on an assistant reply, with the reason of a thumbs down (FEAT-034).

``assess_turn_flags`` keeps one row per rated bot turn (REQ-103) and gains two columns:

- ``rating``: ``down`` (the reply is contested; every row before this revision was one) or
  ``up`` (the student found it useful);
- ``reason_turn_no``: for a ``down`` given with its reason, the STUDENT turn that carries the
  reason. The reason is an ordinary student turn (encrypted, hashed in the chain, answered by the
  assistant); this column only links it to the contested reply, so no text is stored here. A
  ``down`` row from before this revision has none. An ``up`` row never has one, and the reason
  always comes after the reply it contests.

The foreign key to ``assess_turns`` cascades like the turn key's own; at most one flag row names
a given reason turn. The grant matrix does not change (privileges are table-wide), so this
revision carries no ``GRANT_STATEMENTS``. The downgrade drops the columns: an ``up`` row would read
as a flag afterwards, so the ``up`` rows go first.
"""

from __future__ import annotations

from alembic import op

revision = "a0009"
down_revision = "a0008"
branch_labels = None
depends_on = None

TABLE = "public.assess_turn_flags"

UPGRADE = (
    f"ALTER TABLE {TABLE} ADD COLUMN rating text DEFAULT 'down'::text NOT NULL",
    f"ALTER TABLE {TABLE} ADD COLUMN reason_turn_no integer",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_assess_turn_flags_rating "
    "CHECK ((rating = ANY (ARRAY['up'::text, 'down'::text])))",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_assess_turn_flags_reason "
    "CHECK (((reason_turn_no IS NULL) OR ((rating = 'down'::text) AND (reason_turn_no > turn_no))))",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT assess_turn_flags_session_id_reason_turn_no_fkey "
    "FOREIGN KEY (session_id, reason_turn_no) REFERENCES public.assess_turns(session_id, turn_no) "
    "ON DELETE CASCADE",
    "CREATE UNIQUE INDEX ux_assess_turn_flags_reason ON public.assess_turn_flags "
    "(session_id, reason_turn_no) WHERE (reason_turn_no IS NOT NULL)",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE rating = 'up'")
    op.execute("DROP INDEX public.ux_assess_turn_flags_reason")
    op.execute(
        f"ALTER TABLE {TABLE} DROP CONSTRAINT assess_turn_flags_session_id_reason_turn_no_fkey"
    )
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_assess_turn_flags_reason")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_assess_turn_flags_rating")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN reason_turn_no")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN rating")
