"""Session clock in the database (plan assess-session-lifecycle M3.3, M3.6).

- ``assess_sessions.reply_grace_s``: the reply grace (``VEKTRA_ASSESS_REPLY_GRACE_S``) frozen at
  commit, read by ``assess_append_turn``.
- ``assess_append_turn`` (same signature, owner and grants, ``CREATE OR REPLACE``) enforces REQ-070
  on the database's wall clock: a student turn is refused from the deadline on (input lock), an
  ordinary bot reply after deadline + grace; the marked ``interrupted`` bot turn stays accepted
  while the session is ACTIVE, so a pending student turn can always be answered.
- the worker may EXECUTE ``assess_append_turn``: ``reconcile_reply`` (M3.6) appends the marked
  turn of an abandoned reply through the same rules, under the KEK.
- the ``reconcile_reply`` job kind.

Grants: the rendered matrix now includes the SECURITY DEFINER functions' EXECUTE
(``vektra_assess.db.grants.FUNCTIONS``), so this revision carries the whole matrix again, with
EXECUTE on ``assess_append_turn`` for the worker.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

SIG_APPEND = (
    "public.assess_append_turn(uuid, integer, integer, text, text, bytea, text, text)"
)
JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight'"
)

GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_snapshots, assess_start_attempts, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE CREATE ON SCHEMA public FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON query_traces FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "GRANT INSERT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, updated_at, cap_minutes, teacher_config_sealed) ON assess_activity_config TO vektra_assess_api",
    "GRANT SELECT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, teacher_config_set, updated_at, cap_minutes) ON assess_activity_config TO vektra_assess_api",
    "GRANT UPDATE (enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, updated_at, cap_minutes, teacher_config_sealed) ON assess_activity_config TO vektra_assess_api",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_api",
    "GRANT INSERT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_api",
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
    "GRANT INSERT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_state TO vektra_assess_api",
    "GRANT INSERT ON assess_reset_grants TO vektra_assess_api",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_api",
    "GRANT UPDATE ON assess_reset_grants TO vektra_assess_api",
    "GRANT INSERT ON assess_sessions TO vektra_assess_api",
    "GRANT SELECT ON assess_sessions TO vektra_assess_api",
    "GRANT UPDATE (epoch) ON assess_sessions TO vektra_assess_api",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_api",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_api",
    "GRANT SELECT (id, moodle_site_uuid, base_url, release, min_plugin_version, created_at, disabled_at, last_seen_release) ON assess_sites TO vektra_assess_api",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_api",
    "GRANT INSERT ON assess_token_jti TO vektra_assess_api",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_token_jti TO vektra_assess_api",
    "GRANT INSERT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_api",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_render_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_api",
    "GRANT SELECT ON assess_activity_config TO vektra_assess_worker",
    "GRANT UPDATE (teacher_config_sealed, teacher_config_kid) ON assess_activity_config TO vektra_assess_worker",
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
    "GRANT DELETE ON assess_token_jti TO vektra_assess_worker",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcripts TO vektra_assess_worker",
    "GRANT INSERT ON assess_transcripts TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_worker",
    "GRANT UPDATE ON assess_transcripts TO vektra_assess_worker",
    "GRANT DELETE ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT ON assess_turns TO vektra_assess_worker",
    "GRANT SELECT ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT (response_id, namespace_id, steps, total_duration_ms, chunks_retrieved, llm_model, prompt_version, created_at, pipeline) ON query_traces TO vektra_assess_worker",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_definer",
    "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq TO vektra_assess_definer",
    "REVOKE ALL ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_close_session(uuid,integer,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)

_HEADER = """
    CREATE OR REPLACE FUNCTION public.assess_append_turn(
        p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text,
        p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text
    ) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
"""

APPEND_V2 = (
    _HEADER
    + """
    DECLARE
        v_seq integer;
        v_pending timestamptz;
        v_last_no integer;
        v_expected_prev text;
    BEGIN
        IF p_kind NOT IN ('normal', 'interrupted', 'system')
           OR p_role NOT IN ('student', 'bot', 'system') THEN
            RETURN NULL;  -- the opening turn is written by the worker, never here
        END IF;
        SELECT s.seq, s.pending_reply_since INTO v_seq, v_pending
        FROM public.assess_sessions s
        WHERE s.id = p_sid AND s.state = 'ACTIVE' AND s.epoch = p_epoch
        FOR UPDATE;
        IF NOT FOUND OR v_seq <> p_expected_seq THEN
            RETURN NULL;
        END IF;
        IF p_role = 'student' AND v_pending IS NOT NULL THEN
            RETURN NULL;  -- a reply is still pending: one student turn at a time
        END IF;
        SELECT t.turn_no, t.turn_hash INTO v_last_no, v_expected_prev
        FROM public.assess_turns t WHERE t.session_id = p_sid
        ORDER BY t.turn_no DESC LIMIT 1;
        IF NOT FOUND THEN
            RETURN NULL;  -- no opening turn yet: nothing to chain onto
        END IF;
        IF v_expected_prev IS DISTINCT FROM p_prev_hash THEN
            RETURN NULL;
        END IF;
        INSERT INTO public.assess_turns
            (session_id, turn_no, role, kind, ciphertext, turn_hash, prev_hash)
        VALUES (p_sid, v_last_no + 1, p_role, p_kind, p_ciphertext, p_turn_hash,
                p_prev_hash);
        UPDATE public.assess_sessions
           SET seq = seq + 1, last_activity_at = pg_catalog.now(),
               pending_reply_since = CASE
                   WHEN p_role = 'student' THEN pg_catalog.now()
                   WHEN p_role = 'bot' THEN NULL
                   ELSE pending_reply_since END
         WHERE id = p_sid;
        RETURN v_last_no + 1;
    END
    $$
"""
)

APPEND_V3 = (
    _HEADER
    + """
    DECLARE
        v_seq integer;
        v_pending timestamptz;
        v_deadline timestamptz;
        v_grace integer;
        v_last_no integer;
        v_expected_prev text;
    BEGIN
        IF p_kind NOT IN ('normal', 'interrupted', 'system')
           OR p_role NOT IN ('student', 'bot', 'system') THEN
            RETURN NULL;  -- the opening turn is written by the worker, never here
        END IF;
        SELECT s.seq, s.pending_reply_since, s.deadline_at, s.reply_grace_s
          INTO v_seq, v_pending, v_deadline, v_grace
        FROM public.assess_sessions s
        WHERE s.id = p_sid AND s.state = 'ACTIVE' AND s.epoch = p_epoch
        FOR UPDATE;
        IF NOT FOUND OR v_seq <> p_expected_seq THEN
            RETURN NULL;
        END IF;
        -- REQ-070, on the database's wall clock: no student message from the deadline on, and
        -- no ordinary bot reply after deadline + grace; the marked interrupted turn (no content
        -- of the model) is still accepted so that a pending student turn is always answered
        IF v_deadline IS NOT NULL THEN
            IF p_role = 'student' AND pg_catalog.clock_timestamp() >= v_deadline THEN
                RETURN NULL;
            END IF;
            IF p_role = 'bot' AND p_kind = 'normal' AND pg_catalog.clock_timestamp()
               > v_deadline + pg_catalog.make_interval(secs => v_grace) THEN
                RETURN NULL;
            END IF;
        END IF;
        IF p_role = 'student' AND v_pending IS NOT NULL THEN
            RETURN NULL;  -- a reply is still pending: one student turn at a time
        END IF;
        SELECT t.turn_no, t.turn_hash INTO v_last_no, v_expected_prev
        FROM public.assess_turns t WHERE t.session_id = p_sid
        ORDER BY t.turn_no DESC LIMIT 1;
        IF NOT FOUND THEN
            RETURN NULL;  -- no opening turn yet: nothing to chain onto
        END IF;
        IF v_expected_prev IS DISTINCT FROM p_prev_hash THEN
            RETURN NULL;
        END IF;
        INSERT INTO public.assess_turns
            (session_id, turn_no, role, kind, ciphertext, turn_hash, prev_hash)
        VALUES (p_sid, v_last_no + 1, p_role, p_kind, p_ciphertext, p_turn_hash,
                p_prev_hash);
        UPDATE public.assess_sessions
           SET seq = seq + 1, last_activity_at = pg_catalog.now(),
               pending_reply_since = CASE
                   WHEN p_role = 'student' THEN pg_catalog.now()
                   WHEN p_role = 'bot' THEN NULL
                   ELSE pending_reply_since END
         WHERE id = p_sid;
        RETURN v_last_no + 1;
    END
    $$
"""
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE assess_sessions ADD COLUMN reply_grace_s INTEGER NOT NULL DEFAULT 60 "
        "CONSTRAINT ck_assess_sessions_reply_grace CHECK (reply_grace_s >= 0)"
    )
    op.execute(APPEND_V3)
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS}))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM assess_jobs WHERE kind = 'reconcile_reply'")
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK (kind IN ({OLD_JOB_KINDS}))"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG_APPEND} FROM vektra_assess_worker")
    op.execute(APPEND_V2)
    op.execute("ALTER TABLE assess_sessions DROP COLUMN reply_grace_s")
