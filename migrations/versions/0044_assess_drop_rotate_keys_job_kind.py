"""Drop the ``rotate_keys`` job kind (coordinator decision, 2026-11-10).

The kind had no enqueuer and no handler, so it was a production slot that could never be bound,
and the worker refuses to start with an unbound slot: with assess enabled, no production worker
could start. Key rotation stays an operator task: the CLI ``python -m vektra_assess.keys rewrap``
(then ``keys verify``) rewraps every wrapped key with the old and the new keyrings under explicit
operator control. A job doing the same inside the worker would need both keyrings in the worker
process, so it is not written.

The kind leaves the table's closed list, the ``JobKind`` enum and the slot table together; the
list starts from the one revision 0042 left (with ``smtp_test_send``). Upgrade deletes any row of
the kind first (none can exist: nothing ever queued one).

Revision ID: 0044
Revises: 0043
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

JOB_KINDS = (
    "'setup_check', 'gate_refresh', 'render_check', "
    "'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate', 'smtp_test_send'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'gate_refresh', 'render_check', "
    "'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate', 'smtp_test_send'"
)


def upgrade() -> None:
    op.execute("DELETE FROM assess_jobs WHERE kind = 'rotate_keys'")
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK (kind IN ({OLD_JOB_KINDS}))"
    )
