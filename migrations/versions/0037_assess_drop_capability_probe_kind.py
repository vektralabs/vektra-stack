"""Drop the ``capability_probe`` job kind (coordinator decision, 2026-11-08).

The kind had no enqueuer and no specified body: the capability matrix is written by the probes
of every setup-check run (plan assess-moodle-ws, data lifecycle), so a separate kind only added
a production slot that could never be bound. The worker refuses to start with an unbound slot,
so the kind goes from the table's closed list, the ``JobKind`` enum and the slot table together.

Upgrade deletes any row of the kind first (none can exist: nothing ever queued one).

Revision ID: 0037
Revises: 0036
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

JOB_KINDS = (
    "'setup_check', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate'"
)


def upgrade() -> None:
    op.execute("DELETE FROM assess_jobs WHERE kind = 'capability_probe'")
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
