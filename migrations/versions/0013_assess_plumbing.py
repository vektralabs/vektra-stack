"""Assess transcripts, deliveries, job outbox and plumbing tables (plan assess-data-crypto 1.4).

- ``assess_jobs`` is the transactional outbox (a close inserts a ``transcript_generate``
  job in the same transaction). ``args`` is ids only: ``pg_column_size(args) < 1024``.
- ``assess_audit_events`` is append-only (trigger raises on UPDATE; no role gets UPDATE or
  DELETE) and is written inside the business transaction, so an audit failure aborts the
  action (fail closed), unlike ``vektra_shared.audit.log_event``.
- worker-written status tables (render, gate inputs, heartbeat) are content-free.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure'"
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assess_transcripts (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id    UUID NOT NULL UNIQUE REFERENCES assess_sessions(id),
            ciphertext    BYTEA NULL,
            dek_wrapped   BYTEA NULL,
            transcript_kid TEXT NOT NULL,
            content_hash  TEXT NOT NULL,
            generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            shredded_at   TIMESTAMPTZ NULL,
            CONSTRAINT ck_assess_transcripts_shredded
                CHECK ((ciphertext IS NULL) = (shredded_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE TABLE assess_deliveries (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            transcript_id     UUID NOT NULL REFERENCES assess_transcripts(id),
            recipient_hash    TEXT NOT NULL,
            recipient_sealed  BYTEA NULL,
            state             TEXT NOT NULL DEFAULT 'pending',
            message_id        TEXT NULL,
            attempts          INTEGER NOT NULL DEFAULT 0,
            not_before        TIMESTAMPTZ NULL,
            last_error_code   TEXT NULL,
            UNIQUE (transcript_id, recipient_hash),
            CONSTRAINT ck_assess_deliveries_state
                CHECK (state IN ('pending', 'sending', 'sent', 'retrying', 'abandoned'))
        )
    """)
    op.execute(f"""
        CREATE TABLE assess_jobs (
            id           BIGSERIAL PRIMARY KEY,
            kind         TEXT NOT NULL,
            args         JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            dedupe_key   TEXT NULL,
            state        TEXT NOT NULL DEFAULT 'ready',
            not_before   TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_until  TIMESTAMPTZ NULL,
            leased_by    TEXT NULL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            done_at      TIMESTAMPTZ NULL,
            CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS})),
            CONSTRAINT ck_assess_jobs_state
                CHECK (state IN ('ready', 'leased', 'done', 'failed', 'dead')),
            CONSTRAINT ck_assess_jobs_args_small CHECK (pg_column_size(args) < 1024)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_assess_jobs_dedupe
            ON assess_jobs (dedupe_key) WHERE state IN ('ready', 'leased')
    """)
    op.execute("""
        CREATE INDEX ix_assess_jobs_claim
            ON assess_jobs (state, not_before) WHERE state = 'ready'
    """)
    op.execute("""
        ALTER TABLE assess_jobs SET (
            autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_cost_delay = 0
        )
    """)
    op.execute("""
        CREATE TABLE assess_render_checks (
            id              BIGSERIAL PRIMARY KEY,
            site_id         UUID NOT NULL REFERENCES assess_sites(id),
            student_userid  BIGINT NOT NULL,
            cmid            BIGINT NOT NULL,
            requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            done_at         TIMESTAMPTZ NULL
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_assess_render_checks_pending
            ON assess_render_checks (site_id, student_userid, cmid) WHERE done_at IS NULL
    """)
    op.execute("""
        CREATE TABLE assess_render_state (
            site_id         UUID NOT NULL REFERENCES assess_sites(id),
            student_userid  BIGINT NOT NULL,
            cmid            BIGINT NOT NULL,
            status          TEXT NOT NULL,
            reason_code     TEXT NULL,
            computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, student_userid, cmid)
        )
    """)
    op.execute("""
        CREATE TABLE assess_gate_inputs (
            site_id         UUID NOT NULL REFERENCES assess_sites(id),
            student_userid  BIGINT NOT NULL,
            cmid            BIGINT NOT NULL,
            inputs          JSONB NOT NULL DEFAULT '{}'::jsonb,
            refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, student_userid, cmid)
        )
    """)
    op.execute("""
        CREATE TABLE assess_consumed_jtis (
            jti_hash     TEXT PRIMARY KEY,
            aud          TEXT NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            consumed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE assess_transcript_download_tokens (
            jti_hash        TEXT PRIMARY KEY,
            transcript_id   UUID NOT NULL REFERENCES assess_transcripts(id),
            subject_userid  BIGINT NOT NULL,
            expires_at      TIMESTAMPTZ NOT NULL,
            consumed_at     TIMESTAMPTZ NULL
        )
    """)
    op.execute("""
        CREATE TABLE assess_audit_events (
            id           BIGSERIAL PRIMARY KEY,
            site_id      UUID NULL,
            session_id   UUID NULL,
            event        TEXT NOT NULL,
            reason_code  TEXT NULL,
            actor_kind   TEXT NOT NULL,
            data         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_assess_audit_events_data_small CHECK (pg_column_size(data) < 2048)
        )
    """)
    op.execute("""
        CREATE FUNCTION assess_audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'assess_audit_events is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assess_audit_events_append_only
            BEFORE UPDATE OR DELETE ON assess_audit_events
            FOR EACH ROW EXECUTE FUNCTION assess_audit_events_append_only()
    """)
    op.execute("""
        CREATE TABLE assess_erasure_records (
            id                   BIGSERIAL PRIMARY KEY,
            site_id              UUID NOT NULL REFERENCES assess_sites(id),
            subject_userid_hash  TEXT NOT NULL,
            requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at         TIMESTAMPTZ NULL,
            rows_deleted         JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("""
        CREATE TABLE assess_heartbeat (
            component  TEXT PRIMARY KEY,
            beat_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            details    JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_sessions) THEN
                RAISE EXCEPTION 'migration 0013 downgrade refused: assess_sessions holds '
                    'rows (exam evidence). Export and purge them first.';
            END IF;
        END
        $$
    """)
    for table in (
        "assess_heartbeat",
        "assess_erasure_records",
        "assess_audit_events",
        "assess_transcript_download_tokens",
        "assess_consumed_jtis",
        "assess_gate_inputs",
        "assess_render_state",
        "assess_render_checks",
        "assess_jobs",
        "assess_deliveries",
        "assess_transcripts",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION assess_audit_events_append_only()")
