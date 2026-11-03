"""Assess sites, site-bound keys, capability matrix and activity config (plan assess-data-crypto 1.1).

First of four revisions creating the ``assess_*`` schema (ARCH-077). Everything here is
non-sensitive registry and configuration, except ``assess_activity_config.teacher_config_sealed``
which holds an envelope sealed to the worker's public key: the API role never gets a
column grant on it (grant matrix, task 3).

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TABLES = (
    "assess_reset_grants",
    "assess_extra_time",
    "assess_activity_config",
    "assess_setup_checks",
    "assess_site_capabilities",
    "assess_site_keys",
    "assess_sites",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assess_sites (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            moodle_site_uuid    TEXT NOT NULL UNIQUE,
            base_url            TEXT NOT NULL,
            release             TEXT NULL,
            min_plugin_version  TEXT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            disabled_at         TIMESTAMPTZ NULL
        )
    """)
    op.execute("""
        CREATE TABLE assess_site_keys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id     UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            api_key_id  UUID NOT NULL UNIQUE REFERENCES api_keys(id) ON DELETE CASCADE,
            purpose     TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_site_keys_purpose
                CHECK (purpose IN ('mint', 'teacher', 'privacy-export', 'privacy-erase'))
        )
    """)
    op.execute("""
        CREATE TABLE assess_site_capabilities (
            site_id        UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            flag           TEXT NOT NULL,
            release        TEXT NULL,
            state          TEXT NOT NULL,
            evidence_hash  TEXT NULL,
            checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, flag),
            CONSTRAINT ck_assess_site_capabilities_state
                CHECK (state IN ('readable', 'partial', 'unreadable'))
        )
    """)
    op.execute("""
        CREATE TABLE assess_setup_checks (
            id        BIGSERIAL PRIMARY KEY,
            site_id   UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            item      TEXT NOT NULL,
            result    TEXT NOT NULL,
            run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_setup_checks_result CHECK (result IN ('ok', 'missing'))
        )
    """)
    op.execute("""
        CREATE TABLE assess_activity_config (
            id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id                 UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid                    BIGINT NOT NULL,
            activity_type           TEXT NOT NULL,
            enabled                 BOOLEAN NOT NULL DEFAULT false,
            flags                   JSONB NOT NULL DEFAULT '{}'::jsonb,
            rag_enabled             BOOLEAN NOT NULL DEFAULT false,
            end_at                  TIMESTAMPTZ NULL,
            attested_opening_at     TIMESTAMPTZ NULL,
            attested_by             TEXT NULL,
            recipients_extra        TEXT[] NOT NULL DEFAULT '{}',
            teacher_config_sealed   BYTEA NULL,
            teacher_config_kid      TEXT NULL,
            teacher_config_set      BOOLEAN NOT NULL GENERATED ALWAYS AS
                                        (teacher_config_sealed IS NOT NULL) STORED,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (site_id, cmid),
            CONSTRAINT ck_assess_activity_config_type
                CHECK (activity_type IN ('quiz', 'assign')),
            CONSTRAINT ck_assess_activity_config_sealed_kid
                CHECK ((teacher_config_sealed IS NULL) = (teacher_config_kid IS NULL))
        )
    """)
    op.execute("""
        CREATE TABLE assess_extra_time (
            site_id         UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid            BIGINT NOT NULL,
            student_userid  BIGINT NOT NULL,
            extra_seconds   INTEGER NOT NULL,
            granted_by      TEXT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, cmid, student_userid),
            CONSTRAINT ck_assess_extra_time_positive CHECK (extra_seconds > 0)
        )
    """)
    op.execute("""
        CREATE TABLE assess_reset_grants (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id         UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid            BIGINT NOT NULL,
            student_userid  BIGINT NOT NULL,
            granted_by      TEXT NULL,
            granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            consumed_at     TIMESTAMPTZ NULL
        )
    """)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
