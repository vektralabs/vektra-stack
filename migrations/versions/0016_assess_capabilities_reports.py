"""Capability matrix completion and setup-check reports (plan assess-moodle-ws, group D).

Builds on revision 0010 (``assess_site_capabilities`` already exists with ``state``,
``release`` and ``evidence_hash``):

- ``assess_site_capabilities.reason_code``: why a flag is not readable (content-free code);
- a CHECK on ``flag``: the closed list of capability flags (ARCH-070 plus the two additions
  the OQ-025 spike asked for), so a typo can never become a silently-unreadable flag;
- ``assess_setup_check_reports``: one row per REQ-079 setup check run (content-free items);
- ``assess_sites.probe_manifest`` / ``last_seen_release``: the fixture expectations the
  probes compare against, and the release the matrix was last probed on.

Grants for the new objects are in revision 0017, rendered from ``db/grants.py``.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

FLAGS = (
    "quiz_overrides", "assign_extensions", "assign_cutoff", "quiz_grace", "attempt_state",
    "per_student_timelimit", "review_html_filterable", "rubric_display_settings",
    "module_visibility_per_student", "loginas_crosscheck", "graders_resolvable",
    "enrolment_readable", "release_state", "grading_state",
    "assign_user_overrides", "review_options_per_student",
)  # fmt: skip


def upgrade() -> None:
    op.execute("ALTER TABLE assess_site_capabilities ADD COLUMN reason_code TEXT NULL")
    flags = ", ".join(f"'{f}'" for f in FLAGS)
    op.execute(
        f"ALTER TABLE assess_site_capabilities ADD CONSTRAINT ck_assess_site_capabilities_flag "
        f"CHECK (flag IN ({flags}))"
    )
    op.execute("ALTER TABLE assess_sites ADD COLUMN probe_manifest JSONB NULL")
    op.execute("ALTER TABLE assess_sites ADD COLUMN last_seen_release TEXT NULL")
    op.execute("""
        CREATE TABLE assess_setup_check_reports (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id               UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            requested_by_key_id   UUID NULL REFERENCES api_keys(id) ON DELETE SET NULL,
            status                TEXT NOT NULL DEFAULT 'queued',
            moodle_release        TEXT NULL,
            items                 JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at           TIMESTAMPTZ NULL,
            CONSTRAINT ck_assess_setup_check_reports_status
                CHECK (status IN ('queued', 'running', 'done', 'failed')),
            CONSTRAINT ck_assess_setup_check_reports_items_small
                CHECK (pg_column_size(items) < 16384)
        )
    """)
    op.execute(
        "CREATE INDEX ix_assess_setup_check_reports_site ON assess_setup_check_reports "
        "(site_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE assess_setup_check_reports")
    op.execute("ALTER TABLE assess_sites DROP COLUMN last_seen_release")
    op.execute("ALTER TABLE assess_sites DROP COLUMN probe_manifest")
    op.execute(
        "ALTER TABLE assess_site_capabilities DROP CONSTRAINT ck_assess_site_capabilities_flag"
    )
    op.execute("ALTER TABLE assess_site_capabilities DROP COLUMN reason_code")
