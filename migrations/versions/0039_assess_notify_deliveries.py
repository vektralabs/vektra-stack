"""Teacher and operator notifications ride the delivery outbox (plan assess-transcript-delivery,
product decision 2026-11-08: by email through the transcript relay).

``assess_deliveries`` was created with a ``kind`` and a NULL-able ``transcript_id`` for rows that
are not a transcript copy. The two notification job kinds become two more kinds:

- ``kind`` accepts ``teacher_notify`` and ``operator_notify``;
- ``job_id``: the ``assess_jobs`` id of the notification (the first job of a chain of retries), set
  iff the row is a notification (``ck_assess_deliveries_job``). No foreign key: done jobs are
  deleted by the cleanup tick, the delivery row is the record that stays;
- one row per (notification, recipient ref): ``uq_assess_deliveries_job_ref`` (partial: the
  transcript rows keep ``uq_assess_deliveries_round_ref``);
- a notification row is always addressed by ref (``recipient_kind = 'address'``), never by user id.

No privilege changes: the worker already holds every privilege on the table and the API reads the
listed status columns only (``job_id`` is not among them).

Revision ID: 0039
Revises: 0038
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

KINDS = "'transcript', 'reset_notice', 'teacher_notify', 'operator_notify'"
OLD_KINDS = "'transcript', 'reset_notice'"
NOTIFY = "kind IN ('teacher_notify', 'operator_notify')"


def upgrade() -> None:
    op.execute("ALTER TABLE assess_deliveries ADD COLUMN job_id BIGINT NULL")
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_kind"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_kind "
        f"CHECK (kind IN ({KINDS}))"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_job "
        f"CHECK (({NOTIFY}) = (job_id IS NOT NULL) "
        f"AND (NOT ({NOTIFY}) OR recipient_kind = 'address'))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_assess_deliveries_job_ref "
        "ON assess_deliveries (job_id, recipient_ref) WHERE job_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM assess_deliveries WHERE {NOTIFY}")
    op.execute("DROP INDEX uq_assess_deliveries_job_ref")
    op.execute("ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_job")
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_kind"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_kind "
        f"CHECK (kind IN ({OLD_KINDS}))"
    )
    op.execute("ALTER TABLE assess_deliveries DROP COLUMN job_id")
