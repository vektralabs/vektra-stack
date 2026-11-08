"""Teacher configuration: sealed per field, versioned, RAG locked off (plan assess-teacher-ops 11a).

- ``assess_activity_config_sealed(site_id, cmid, field, ...)``: one row per secret field
  (``solution``, ``pasted_rubric``, ``transcript_recipients``), each sealed to the worker's public
  key under its own binding ``[site_id, cmid, field]`` (ARCH-071, OQ-034). A PUT changes one field
  (absent = unchanged, null = clear, string = replace) and the API can never read the others back,
  so one composite blob could not be merged. A cleared field keeps its row with a NULL blob
  (``is_set`` false). The API may INSERT/UPDATE the row but SELECT only which fields are set
  (``is_set``, ``item_count``), never ``sealed_blob`` nor ``refs`` (the recipient refs).
- ``assess_audit_sealed_change()``: SECURITY DEFINER trigger (nobody may EXECUTE it) that writes
  ``assess_teacher_config_changed`` for a teacher's write of a sealed field, old/new from the row
  itself: set / replaced / cleared, and for the recipients the old and new refs (the API cannot
  read the old ones). It fires on ``UPDATE OF updated_at``, which a key rotation never sets.
- ``assess_teacher_gate(site_id, cmid, inputs, refreshed_at)``: the activity-level facts the
  ``teacher_gate`` job (new job kind) writes: editors, graders and the REQ-077 enable facts. The
  teacher endpoints accept them only while fresh (the API reads, the worker writes).
- ``assess_activity_config``: ``version`` (optimistic concurrency of the config PUT),
  ``updated_by`` (Moodle user id of the last editor), ``CHECK (rag_enabled = false)`` (ARCH-072:
  RAG ON is unrepresentable in this release). The composite ``teacher_config_sealed`` /
  ``_kid`` / ``_set`` and the never-read ``recipients_extra`` are dropped (no dormant columns).
  The upgrade refuses a database where a composite config was ever written: no release wrote it
  (the PUT arrives with this revision), and it cannot be split without the worker's key.

Revision ID: 0036
Revises: 0035
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_AUDIT = "public.assess_audit_sealed_change()"

JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify', 'teacher_gate'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify', 'operator_notify'"
)

GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_activity_config_sealed, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_teacher_gate, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE CREATE ON SCHEMA public FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON query_traces FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "GRANT INSERT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT SELECT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT UPDATE (enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT INSERT (site_id, cmid, field, sealed_blob, kid, refs, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT SELECT (site_id, cmid, field, is_set, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT UPDATE (sealed_blob, kid, refs, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_api",
    "GRANT INSERT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT (id, transcript_id, kind, round, recipient_kind, recipient_userid, status, attempts, not_before, attempt_started_at, last_error_class, smtp_code, sent_at, created_at) ON assess_deliveries TO vektra_assess_api",
    "GRANT SELECT ON assess_enrolment_checks TO vektra_assess_api",
    "GRANT INSERT ON assess_erasure_records TO vektra_assess_api",
    "GRANT INSERT ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_api",
    "GRANT UPDATE ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_api",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_api",
    "GRANT INSERT ON assess_jobs TO vektra_assess_api",
    "GRANT INSERT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_api",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_state TO vektra_assess_api",
    "GRANT INSERT ON assess_reset_grants TO vektra_assess_api",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_api",
    "GRANT UPDATE ON assess_reset_grants TO vektra_assess_api",
    "GRANT INSERT ON assess_sessions TO vektra_assess_api",
    "GRANT SELECT ON assess_sessions TO vektra_assess_api",
    "GRANT UPDATE (epoch, tokens_used) ON assess_sessions TO vektra_assess_api",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_api",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_api",
    "GRANT SELECT (id, moodle_site_uuid, base_url, release, min_plugin_version, created_at, disabled_at, last_seen_release, moodle_origin) ON assess_sites TO vektra_assess_api",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_api",
    "GRANT SELECT ON assess_teacher_gate TO vektra_assess_api",
    "GRANT INSERT ON assess_token_jti TO vektra_assess_api",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_token_jti TO vektra_assess_api",
    "GRANT INSERT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_api",
    "GRANT INSERT ON assess_turn_flags TO vektra_assess_api",
    "GRANT SELECT ON assess_turn_flags TO vektra_assess_api",
    "GRANT INSERT ON assess_turn_notes TO vektra_assess_api",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_render_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_api",
    "GRANT SELECT ON assess_activity_config TO vektra_assess_worker",
    "GRANT SELECT ON assess_activity_config_sealed TO vektra_assess_worker",
    "GRANT UPDATE (sealed_blob, kid) ON assess_activity_config_sealed TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_worker",
    "GRANT DELETE ON assess_consumed_jtis TO vektra_assess_worker",
    "GRANT SELECT ON assess_consumed_jtis TO vektra_assess_worker",
    "GRANT DELETE ON assess_deliveries TO vektra_assess_worker",
    "GRANT INSERT ON assess_deliveries TO vektra_assess_worker",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_worker",
    "GRANT UPDATE ON assess_deliveries TO vektra_assess_worker",
    "GRANT DELETE ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_erasure_records TO vektra_assess_worker",
    "GRANT SELECT ON assess_erasure_records TO vektra_assess_worker",
    "GRANT UPDATE ON assess_erasure_records TO vektra_assess_worker",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_worker",
    "GRANT DELETE ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT INSERT ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT UPDATE ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT DELETE ON assess_heartbeat TO vektra_assess_worker",
    "GRANT INSERT ON assess_heartbeat TO vektra_assess_worker",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_worker",
    "GRANT UPDATE ON assess_heartbeat TO vektra_assess_worker",
    "GRANT DELETE ON assess_jobs TO vektra_assess_worker",
    "GRANT INSERT ON assess_jobs TO vektra_assess_worker",
    "GRANT SELECT ON assess_jobs TO vektra_assess_worker",
    "GRANT UPDATE ON assess_jobs TO vektra_assess_worker",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_worker",
    "GRANT DELETE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT UPDATE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_checks TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_state TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_state TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_state TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_state TO vektra_assess_worker",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_worker",
    "GRANT UPDATE (consumed_at) ON assess_reset_grants TO vektra_assess_worker",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_worker",
    "GRANT INSERT ON assess_session_handoff TO vektra_assess_worker",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_worker",
    "GRANT UPDATE ON assess_session_handoff TO vektra_assess_worker",
    "GRANT DELETE ON assess_session_keys TO vektra_assess_worker",
    "GRANT INSERT ON assess_session_keys TO vektra_assess_worker",
    "GRANT SELECT ON assess_session_keys TO vektra_assess_worker",
    "GRANT UPDATE ON assess_session_keys TO vektra_assess_worker",
    "GRANT DELETE ON assess_sessions TO vektra_assess_worker",
    "GRANT INSERT ON assess_sessions TO vektra_assess_worker",
    "GRANT SELECT ON assess_sessions TO vektra_assess_worker",
    "GRANT UPDATE ON assess_sessions TO vektra_assess_worker",
    "GRANT DELETE ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT UPDATE ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT DELETE ON assess_setup_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT INSERT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT UPDATE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_worker",
    "GRANT SELECT ON assess_sites TO vektra_assess_worker",
    "GRANT UPDATE (release, min_plugin_version, last_seen_release, probe_manifest) ON assess_sites TO vektra_assess_worker",
    "GRANT DELETE ON assess_snapshots TO vektra_assess_worker",
    "GRANT INSERT ON assess_snapshots TO vektra_assess_worker",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_worker",
    "GRANT DELETE ON assess_start_attempts TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT DELETE ON assess_start_breaker TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT UPDATE ON assess_start_breaker TO vektra_assess_worker",
    "GRANT DELETE ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT INSERT ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT SELECT ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT UPDATE ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT DELETE ON assess_token_jti TO vektra_assess_worker",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcripts TO vektra_assess_worker",
    "GRANT INSERT ON assess_transcripts TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_worker",
    "GRANT UPDATE ON assess_transcripts TO vektra_assess_worker",
    "GRANT DELETE ON assess_turn_flags TO vektra_assess_worker",
    "GRANT SELECT ON assess_turn_flags TO vektra_assess_worker",
    "GRANT DELETE ON assess_turn_notes TO vektra_assess_worker",
    "GRANT SELECT ON assess_turn_notes TO vektra_assess_worker",
    "GRANT DELETE ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT ON assess_turns TO vektra_assess_worker",
    "GRANT SELECT ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT (response_id, namespace_id, steps, total_duration_ms, chunks_retrieved, llm_model, prompt_version, created_at, pipeline) ON query_traces TO vektra_assess_worker",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_definer",
    "GRANT INSERT ON assess_deliveries TO vektra_assess_definer",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_definer",
    "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT UPDATE (ended_at, credited_s) ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since, epoch, credited_outage_s, deadline_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq TO vektra_assess_definer",
    "REVOKE ALL ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_audit_sealed_change() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_close_session(uuid,integer,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_end_outage(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_end_outage(uuid,integer) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)

AUDIT_FN = """
    CREATE FUNCTION public.assess_audit_sealed_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_action text;
        v_data jsonb;
        v_old text[] := '{}';
        v_new text[] := coalesce(NEW.refs, '{}');
        i integer;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.sealed_blob IS NULL THEN
                RETURN NULL;  -- clearing a field that was never set: nothing changed
            END IF;
            v_action := 'set';
        ELSE
            v_old := coalesce(OLD.refs, '{}');
            IF OLD.sealed_blob IS NULL AND NEW.sealed_blob IS NULL THEN
                RETURN NULL;
            END IF;
            -- the same recipients sent again (the form re-sends them): re-sealed, not changed
            IF OLD.sealed_blob IS NOT NULL AND NEW.sealed_blob IS NOT NULL
               AND NEW.field = 'transcript_recipients' AND v_old = v_new THEN
                RETURN NULL;
            END IF;
            v_action := CASE WHEN NEW.sealed_blob IS NULL THEN 'cleared'
                             WHEN OLD.sealed_blob IS NULL THEN 'set'
                             ELSE 'replaced' END;
        END IF;
        v_data := pg_catalog.jsonb_build_object(
            'field', NEW.field, 'action', v_action, 'cmid', NEW.cmid, 'actor', NEW.updated_by);
        IF NEW.field = 'transcript_recipients' THEN
            v_data := v_data || pg_catalog.jsonb_build_object(
                'old_count', pg_catalog.cardinality(v_old),
                'new_count', pg_catalog.cardinality(v_new));
            FOR i IN 1 .. pg_catalog.cardinality(v_old) LOOP
                v_data := v_data || pg_catalog.jsonb_build_object('old_' || i, v_old[i]);
            END LOOP;
            FOR i IN 1 .. pg_catalog.cardinality(v_new) LOOP
                v_data := v_data || pg_catalog.jsonb_build_object('new_' || i, v_new[i]);
            END LOOP;
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (NEW.site_id, NULL, 'assess_teacher_config_changed', NEW.field, 'api', v_data,
                pg_catalog.clock_timestamp());
        RETURN NULL;
    END
    $$
"""


def upgrade() -> None:
    # no release ever wrote the composite config (the PUT arrives with this revision); one that
    # exists cannot be split into fields without the worker's private key: refuse, never drop it
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_activity_config
                        WHERE teacher_config_sealed IS NOT NULL) THEN
                RAISE EXCEPTION 'assess_activity_config holds a composite sealed teacher config: '
                    're-enter it after the upgrade (clear teacher_config_sealed first)';
            END IF;
        END
        $$
    """)
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN teacher_config_set")
    op.execute(
        "ALTER TABLE assess_activity_config DROP CONSTRAINT ck_assess_activity_config_sealed_kid"
    )
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN teacher_config_sealed")
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN teacher_config_kid")
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN recipients_extra")
    op.execute(
        "ALTER TABLE assess_activity_config ADD COLUMN version INTEGER NOT NULL DEFAULT 0 "
        "CONSTRAINT ck_assess_activity_config_version CHECK (version >= 0)"
    )
    op.execute("ALTER TABLE assess_activity_config ADD COLUMN updated_by BIGINT NULL")
    op.execute(
        "ALTER TABLE assess_activity_config ADD CONSTRAINT ck_assess_activity_config_rag_off "
        "CHECK (rag_enabled = false)"
    )
    op.execute("""
        CREATE TABLE assess_activity_config_sealed (
            site_id      UUID NOT NULL,
            cmid         BIGINT NOT NULL,
            field        TEXT NOT NULL,
            sealed_blob  BYTEA NULL,
            kid          TEXT NULL,
            is_set       BOOLEAN NOT NULL GENERATED ALWAYS AS (sealed_blob IS NOT NULL) STORED,
            refs         TEXT[] NULL,
            item_count   INTEGER NULL,
            updated_by   BIGINT NULL,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, cmid, field),
            FOREIGN KEY (site_id, cmid) REFERENCES assess_activity_config (site_id, cmid)
                ON DELETE CASCADE,
            CONSTRAINT ck_assess_activity_config_sealed_field
                CHECK (field IN ('solution', 'pasted_rubric', 'transcript_recipients')),
            CONSTRAINT ck_assess_activity_config_sealed_kid
                CHECK ((sealed_blob IS NULL) = (kid IS NULL)),
            CONSTRAINT ck_assess_activity_config_sealed_refs CHECK (
                (refs IS NULL AND item_count IS NULL)
                OR (field = 'transcript_recipients' AND sealed_blob IS NOT NULL
                    AND item_count BETWEEN 1 AND 3 AND cardinality(refs) = item_count))
        )
    """)
    # the worker-written activity-level facts a teacher request is verified against: editors,
    # graders and the REQ-077 enable facts of (site, cmid), ids and flags only
    op.execute("""
        CREATE TABLE assess_teacher_gate (
            site_id       UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid          BIGINT NOT NULL,
            inputs        JSONB NOT NULL DEFAULT '{}'::jsonb,
            refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (site_id, cmid),
            CONSTRAINT ck_assess_teacher_gate_small CHECK (pg_column_size(inputs) < 16384)
        )
    """)
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS}))"
    )
    op.execute(AUDIT_FN)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema (revoked again by the matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {SIG_AUDIT} OWNER TO {DEFINER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG_AUDIT} FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER trg_assess_audit_sealed_change AFTER INSERT OR UPDATE OF updated_at "
        "ON assess_activity_config_sealed FOR EACH ROW "
        "EXECUTE FUNCTION public.assess_audit_sealed_change()"
    )
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    from importlib import util
    from pathlib import Path

    # the per-field sealed rows cannot be composed back without the worker's key: they are lost
    op.execute("DROP TABLE assess_activity_config_sealed")
    op.execute("DELETE FROM assess_jobs WHERE kind = 'teacher_gate'")
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK (kind IN ({OLD_JOB_KINDS}))"
    )
    op.execute("DROP TABLE assess_teacher_gate")
    op.execute(f"DROP FUNCTION {SIG_AUDIT}")
    op.execute(
        "ALTER TABLE assess_activity_config DROP CONSTRAINT ck_assess_activity_config_rag_off"
    )
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN updated_by")
    op.execute("ALTER TABLE assess_activity_config DROP COLUMN version")
    op.execute(
        "ALTER TABLE assess_activity_config ADD COLUMN recipients_extra TEXT[] NOT NULL "
        "DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE assess_activity_config ADD COLUMN teacher_config_sealed BYTEA NULL"
    )
    op.execute(
        "ALTER TABLE assess_activity_config ADD COLUMN teacher_config_kid TEXT NULL"
    )
    op.execute(
        "ALTER TABLE assess_activity_config ADD CONSTRAINT ck_assess_activity_config_sealed_kid "
        "CHECK ((teacher_config_sealed IS NULL) = (teacher_config_kid IS NULL))"
    )
    op.execute(
        "ALTER TABLE assess_activity_config ADD COLUMN teacher_config_set BOOLEAN NOT NULL "
        "GENERATED ALWAYS AS (teacher_config_sealed IS NOT NULL) STORED"
    )
    spec = util.spec_from_file_location(
        "rev_0035", Path(__file__).with_name("0035_assess_delivery_recipient_ref.py")
    )
    assert spec is not None and spec.loader is not None
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for stmt in mod.GRANT_STATEMENTS:
        op.execute(stmt)
