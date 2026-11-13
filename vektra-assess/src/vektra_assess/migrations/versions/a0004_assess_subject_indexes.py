"""Indexes for the subject export's per-subject reads (``GET /privacy/subjects/{userid}/activities``).

The export finds a subject's activities in every ``SUBJECT_TABLES`` table by
``(site_id, student_userid)``, then reads one page by ``cmid = ANY(...)``. Two tables had no
index that serves that filter:

- ``assess_token_jti``: only its primary key (``jti``) and ``expires_at``;
- ``assess_render_checks``: only a partial unique index on the pending checks
  (``done_at IS NULL``), so every completed check was a sequential scan.

Both get ``(site_id, student_userid, cmid)``. No grant or data change. Revision id ``a0004``:
``a0002`` (RLS) and ``a0003`` (erasure alerts) belong to parallel branches; this one chains after
the assess head of its own base and is re-pointed at merge time (``down_revision``).
"""

from __future__ import annotations

from alembic import op

revision = "a0004"
down_revision = "a0001"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_assess_token_jti_subject", "assess_token_jti"),
    ("ix_assess_render_checks_subject", "assess_render_checks"),
)


def upgrade() -> None:
    for name, table in INDEXES:
        op.execute(
            f"CREATE INDEX {name} ON public.{table} (site_id, student_userid, cmid)"
        )


def downgrade() -> None:
    for name, _table in INDEXES:
        op.execute(f"DROP INDEX public.{name}")
