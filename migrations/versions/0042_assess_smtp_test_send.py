"""SMTP test send for the operator (plan assess-privacy-retention 12b, REQ-080).

- ``assess_jobs.kind`` accepts ``smtp_test_send``: the API queues one (``POST
  /api/v1/assess/ops/smtp-test``, admin key), the worker sends one fixed, content-free message to
  the configured operator address. No recipient is ever taken from the request.
- ``assess_deliveries.kind`` accepts ``smtp_test``: the result row. Its id is the test id the API
  generated and returned (the API cannot read jobs back), so ``GET .../smtp-test/{id}`` reads
  the outcome over the columns it already may read (status and error class). It is addressed by
  ref like every configured address, has no transcript and no job id. The purge tick deletes
  these rows after 7 days.

No privilege changes.

Revision ID: 0042
Revises: 0041
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

JOB_KINDS = (
    "'setup_check', 'gate_refresh', 'render_check', "
    "'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate', 'smtp_test_send'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'gate_refresh', 'render_check', "
    "'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate'"
)
KINDS = "'transcript', 'reset_notice', 'teacher_notify', 'operator_notify', 'smtp_test'"
OLD_KINDS = "'transcript', 'reset_notice', 'teacher_notify', 'operator_notify'"


def upgrade() -> None:
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS}))"
    )
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_kind"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_kind "
        f"CHECK (kind IN ({KINDS}))"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_smtp_test "
        "CHECK (kind <> 'smtp_test' OR recipient_kind = 'address')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM assess_deliveries WHERE kind = 'smtp_test'")
    op.execute("DELETE FROM assess_jobs WHERE kind = 'smtp_test_send'")
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_smtp_test"
    )
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_kind"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_kind "
        f"CHECK (kind IN ({OLD_KINDS}))"
    )
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        "ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK (kind IN ({OLD_JOB_KINDS}))"
    )
