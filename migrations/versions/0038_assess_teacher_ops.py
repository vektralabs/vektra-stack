"""Teacher operations: reset, terminate, the reset notice (plan assess-teacher-ops 11b).

- ``assess_reset_grants`` (revision 0010, read by the START gates since plan 8, never written
  before this revision) becomes the REQ-087 reset record: ``session_no`` (the CLOSED session the
  reset follows; UNIQUE per student and activity, so two concurrent resets of the same session
  yield one), ``reason_ct`` / ``reason_kid`` (the teacher's reason, encrypted under the transcript
  KEK: readable by the API for the grader list, never in the audit table), and
  ``include_previous_turns`` (the continuity choice, REQ-087 (b)). ``granted_by`` becomes
  mandatory. The API loses UPDATE on the table: a reset is never rewritten. The upgrade refuses
  a table with rows (none was ever written: nothing could set the new mandatory columns).
- ``assess_terminations``: one row per REQ-081 terminate (reason encrypted the same way, the
  counts it closed and rolled back, whether it also disabled the activity). Audit rows carry only
  its id.
- ``assess_deliveries.reset_id``: a ``reset_notice`` row (REQ-092) names the reset it announces,
  at most one per reset; a ``reset_notice`` row is addressed to a student and has a reset, every
  other row has none. The API may INSERT the notice row (kind, recipient kind, student id, reset
  id), never an address.
- ``assess_rollback_starting(uuid)``: SECURITY DEFINER, EXECUTE for the API only. Deletes ONE
  session that is still STARTING (pre-flight not committed: nothing consumed, the student is
  ELIGIBLE again), verifies by count that nothing of it is left, records the ``rolled_back``
  attempt with reason ``terminated_by_teacher`` (not a system rollback: it counts towards neither
  the abuse cap nor the Start breaker) and the ``assess_start_rollback`` audit row. Any other
  state: false, nothing touched.

Revision ID: 0038
Revises: 0036 (TEMPORARY: becomes 0037 once fix/assess-unbound-worker-slots is merged)
"""

from alembic import op

revision = "0038"
down_revision = "0036"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_ROLLBACK = "public.assess_rollback_starting(uuid)"

GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_activity_config_sealed, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_teacher_gate, assess_terminations, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
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
    "GRANT INSERT (kind, recipient_kind, recipient_userid, reset_id) ON assess_deliveries TO vektra_assess_api",
    "GRANT SELECT (id, transcript_id, kind, round, recipient_kind, recipient_userid, status, attempts, not_before, attempt_started_at, last_error_class, smtp_code, sent_at, created_at, reset_id) ON assess_deliveries TO vektra_assess_api",
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
    "GRANT INSERT ON assess_terminations TO vektra_assess_api",
    "GRANT SELECT ON assess_terminations TO vektra_assess_api",
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
    "GRANT UPDATE (consumed_at, reason_ct, reason_kid) ON assess_reset_grants TO vektra_assess_worker",
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
    "GRANT SELECT ON assess_terminations TO vektra_assess_worker",
    "GRANT UPDATE (reason_ct, reason_kid) ON assess_terminations TO vektra_assess_worker",
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
    "GRANT DELETE ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since, epoch, credited_outage_s, deadline_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_definer",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq, assess_start_attempts_id_seq TO vektra_assess_definer",
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
    "REVOKE ALL ON FUNCTION public.assess_rollback_starting(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_rollback_starting(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)

ROLLBACK_FN = """
    CREATE FUNCTION public.assess_rollback_starting(p_sid uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_site uuid;
        v_cmid bigint;
        v_user bigint;
        v_no integer;
        v_left bigint;
    BEGIN
        DELETE FROM public.assess_sessions WHERE id = p_sid AND state = 'STARTING'
        RETURNING site_id, cmid, student_userid, session_no INTO v_site, v_cmid, v_user, v_no;
        IF NOT FOUND THEN
            RETURN false;  -- committed meanwhile, already rolled back, or never STARTING
        END IF;
        -- verified by count, never assumed from the cascade (as the worker's rollback does)
        SELECT (SELECT count(*) FROM public.assess_sessions WHERE id = p_sid)
             + (SELECT count(*) FROM public.assess_snapshots WHERE session_id = p_sid)
             + (SELECT count(*) FROM public.assess_turns WHERE session_id = p_sid)
             + (SELECT count(*) FROM public.assess_session_handoff WHERE session_id = p_sid)
          INTO v_left;
        IF v_left <> 0 THEN
            RAISE EXCEPTION 'assess_rollback_starting: % row(s) of the session survived', v_left;
        END IF;
        INSERT INTO public.assess_start_attempts
            (site_id, cmid, student_userid, outcome, reason_code, session_id)
        VALUES (v_site, v_cmid, v_user, 'rolled_back', 'terminated_by_teacher', p_sid);
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_start_rollback', 'terminated_by_teacher', 'api',
                pg_catalog.jsonb_build_object('cmid', v_cmid, 'session_no', v_no),
                pg_catalog.clock_timestamp());
        RETURN true;
    END
    $$
"""


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_reset_grants) THEN
                RAISE EXCEPTION 'assess_reset_grants holds rows that no release could write: '
                    'they have no reason, session number or continuity choice';
            END IF;
        END
        $$
    """)
    op.execute("ALTER TABLE assess_reset_grants ALTER COLUMN granted_by SET NOT NULL")
    op.execute(
        "ALTER TABLE assess_reset_grants ADD COLUMN session_no INTEGER NOT NULL "
        "CONSTRAINT ck_assess_reset_grants_session_no CHECK (session_no >= 1)"
    )
    op.execute("ALTER TABLE assess_reset_grants ADD COLUMN reason_ct BYTEA NOT NULL")
    op.execute("ALTER TABLE assess_reset_grants ADD COLUMN reason_kid TEXT NOT NULL")
    op.execute(
        "ALTER TABLE assess_reset_grants ADD COLUMN include_previous_turns BOOLEAN NOT NULL "
        "DEFAULT false"
    )
    op.execute(
        "ALTER TABLE assess_reset_grants ADD CONSTRAINT uq_assess_reset_grants_session "
        "UNIQUE (site_id, cmid, student_userid, session_no)"
    )
    op.execute("""
        CREATE TABLE assess_terminations (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id            UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid               BIGINT NOT NULL,
            reason_ct          BYTEA NOT NULL,
            reason_kid         TEXT NOT NULL,
            closed_count       INTEGER NOT NULL CHECK (closed_count >= 0),
            rolled_back_count  INTEGER NOT NULL CHECK (rolled_back_count >= 0),
            also_disable       BOOLEAN NOT NULL,
            created_by         TEXT NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_assess_terminations_activity ON assess_terminations (site_id, cmid)"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD COLUMN reset_id UUID NULL "
        "REFERENCES assess_reset_grants(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT uq_assess_deliveries_reset "
        "UNIQUE (reset_id)"
    )
    op.execute(
        "ALTER TABLE assess_deliveries ADD CONSTRAINT ck_assess_deliveries_reset CHECK ("
        "(kind = 'reset_notice') = (reset_id IS NOT NULL) "
        "AND (kind <> 'reset_notice' OR recipient_kind = 'student'))"
    )
    op.execute(ROLLBACK_FN)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema (revoked again by the matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {SIG_ROLLBACK} OWNER TO {DEFINER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG_ROLLBACK} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    from importlib import util
    from pathlib import Path

    op.execute(f"DROP FUNCTION {SIG_ROLLBACK}")
    # the notices and resets written by this revision cannot exist without its columns
    op.execute("DELETE FROM assess_deliveries WHERE kind = 'reset_notice'")
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT ck_assess_deliveries_reset"
    )
    op.execute(
        "ALTER TABLE assess_deliveries DROP CONSTRAINT uq_assess_deliveries_reset"
    )
    op.execute("ALTER TABLE assess_deliveries DROP COLUMN reset_id")
    op.execute("DROP TABLE assess_terminations")
    op.execute("DELETE FROM assess_reset_grants")
    op.execute(
        "ALTER TABLE assess_reset_grants DROP CONSTRAINT uq_assess_reset_grants_session"
    )
    for column in ("include_previous_turns", "reason_kid", "reason_ct", "session_no"):
        op.execute(f"ALTER TABLE assess_reset_grants DROP COLUMN {column}")
    op.execute("ALTER TABLE assess_reset_grants ALTER COLUMN granted_by DROP NOT NULL")
    spec = util.spec_from_file_location(
        "rev_0036", Path(__file__).with_name("0036_assess_teacher_config.py")
    )
    assert spec is not None and spec.loader is not None
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for stmt in mod.GRANT_STATEMENTS:
        op.execute(stmt)
