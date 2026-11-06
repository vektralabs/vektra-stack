"""Provider outage in the database (plan assess-session-lifecycle M4.1, M4.2, M4.5; REQ-086).

- ``assess_outage_incidents.credited_s``: the seconds this interval added to the deadline.
- ``assess_enter_outage(sid, epoch, started_at, reason)`` (SECURITY DEFINER, API): ACTIVE ->
  OUTAGE at the session's epoch. It records the interval from the server-side
  ``ProviderCallRecord`` (``started_at`` is the failed call's start, clamped to the commit, the end
  of the previous interval and ``now()``), writes ``assess_outage_start`` and keeps the pending
  student turn. The third consecutive failed call (intervals since the last answered turn) closes
  the session ``service_failure`` instead. Returns ``outage``, ``closed`` or ``noop``.
- ``assess_end_outage(sid, min_credit_s)`` (SECURITY DEFINER, worker probe): OUTAGE -> ACTIVE.
  The open interval ends at ``now()``; credit = its length when at least ``min_credit_s`` (G),
  else 0, capped so the cumulative credit stays within C = min(900 s, duration / 2); the deadline
  is recomputed by the one formula (``vektra_assess.timing.DEADLINE_SQL``); a pending turn's
  ``pending_reply_since`` restarts, so the API's ``PendingTurnDriver`` answers it before
  ``reconcile_reply`` would mark it. Cumulative outage above C closes ``service_failure``
  instead. Returns the credited seconds, -1 when it closed, NULL when the session is not in
  OUTAGE.
- ``assess_close_session`` (same signature): a ``service_failure`` close also enqueues the
  ``teacher_notify`` job in the same transaction (delivery: plan assess-transcript-delivery).
- the ``teacher_notify`` job kind.

The whole grant matrix is re-rendered from ``vektra_assess.db.grants``.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_ENTER = "public.assess_enter_outage(uuid, integer, timestamp with time zone, text)"
SIG_END = "public.assess_end_outage(uuid, integer)"

JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply', "
    "'teacher_notify'"
)
OLD_JOB_KINDS = (
    "'setup_check', 'capability_probe', 'gate_refresh', 'render_check', "
    "'session_deadline', 'transcript_generate', 'transcript_deliver', "
    "'enrolment_recheck', 'purge', 'rotate_keys', 'erasure', 'preflight', 'reconcile_reply'"
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
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_api",
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
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT UPDATE (ended_at, credited_s) ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since, epoch, credited_outage_s, deadline_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq TO vektra_assess_definer",
    "REVOKE ALL ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_close_session(uuid,integer,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_end_outage(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_end_outage(uuid,integer) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)

ENTER = """
    CREATE FUNCTION public.assess_enter_outage(
        p_sid uuid, p_epoch integer, p_started_at timestamptz, p_reason text
    ) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
        v_committed timestamptz;
        v_last_ok timestamptz;
        v_last_end timestamptz;
        v_failed integer;
        v_start timestamptz;
    BEGIN
        SELECT s.state, s.epoch, s.site_id, s.committed_at
          INTO v_state, v_epoch, v_site, v_committed
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND OR v_epoch <> p_epoch THEN
            RETURN 'noop';
        END IF;
        IF v_state = 'OUTAGE' THEN
            RETURN 'outage';  -- idempotent: one interval at a time
        END IF;
        IF v_state <> 'ACTIVE' THEN
            RETURN 'noop';
        END IF;
        -- consecutive failed calls: intervals opened since the last answered bot turn
        SELECT max(t.created_at) INTO v_last_ok FROM public.assess_turns t
         WHERE t.session_id = p_sid AND t.role = 'bot' AND t.kind IN ('opening', 'normal');
        SELECT count(*) + 1 INTO v_failed
          FROM public.assess_outage_incidents i
         WHERE i.session_id = p_sid
           AND i.started_at >= coalesce(v_last_ok, v_committed, '-infinity'::timestamptz);
        IF v_failed >= 3 THEN
            PERFORM public.assess_close_session(p_sid, p_epoch, 'service_failure');
            RETURN 'closed';
        END IF;
        SELECT max(i.ended_at) INTO v_last_end
          FROM public.assess_outage_incidents i WHERE i.session_id = p_sid;
        v_start := least(pg_catalog.now(), greatest(
            coalesce(p_started_at, pg_catalog.now()),
            coalesce(v_committed, '-infinity'::timestamptz),
            coalesce(v_last_end, '-infinity'::timestamptz)));
        UPDATE public.assess_sessions SET state = 'OUTAGE' WHERE id = p_sid;
        INSERT INTO public.assess_outage_incidents (session_id, started_at, reason_code)
        VALUES (p_sid, v_start, left(p_reason, 64));
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_outage_start', left(p_reason, 64), 'api',
                pg_catalog.jsonb_build_object('failed_calls', v_failed),
                pg_catalog.clock_timestamp());
        RETURN 'outage';
    END
    $$
"""

END = """
    CREATE FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer)
    RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
        v_duration integer;
        v_credited integer;
        v_cap integer;
        v_spent double precision;
        v_open_id bigint;
        v_open_s double precision;
        v_credit integer;
    BEGIN
        SELECT s.state, s.epoch, s.site_id, s.duration_s, s.credited_outage_s
          INTO v_state, v_epoch, v_site, v_duration, v_credited
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND OR v_state <> 'OUTAGE' THEN
            RETURN NULL;
        END IF;
        -- C = min(15 min, 50% of the duration): vektra_assess.timing.outage_cap_s
        v_cap := least(900, coalesce(v_duration, 0) / 2);
        SELECT coalesce(sum(extract(epoch FROM coalesce(i.ended_at, pg_catalog.now()) - i.started_at)), 0)
          INTO v_spent FROM public.assess_outage_incidents i WHERE i.session_id = p_sid;
        IF v_spent > v_cap THEN
            PERFORM public.assess_close_session(p_sid, v_epoch, 'service_failure');
            RETURN -1;
        END IF;
        SELECT i.id, extract(epoch FROM pg_catalog.now() - i.started_at)
          INTO v_open_id, v_open_s
          FROM public.assess_outage_incidents i
         WHERE i.session_id = p_sid AND i.ended_at IS NULL
         ORDER BY i.started_at DESC LIMIT 1;
        v_credit := 0;
        IF v_open_id IS NOT NULL AND v_open_s >= p_min_credit_s THEN
            v_credit := least(floor(v_open_s)::integer, greatest(v_cap - v_credited, 0));
        END IF;
        UPDATE public.assess_outage_incidents
           SET ended_at = pg_catalog.now(), credited_s = v_credit
         WHERE id = v_open_id;
        UPDATE public.assess_sessions
           SET state = 'ACTIVE',
               credited_outage_s = credited_outage_s + v_credit,
               -- DEADLINE_SQL with the new credit (SET reads the old column values)
               deadline_at = committed_at
                   + make_interval(secs => duration_s + credited_outage_s + v_credit),
               pending_reply_since = CASE WHEN pending_reply_since IS NULL THEN NULL
                                          ELSE pg_catalog.now() END
         WHERE id = p_sid;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_outage_end', NULL, 'worker',
                pg_catalog.jsonb_build_object('credited_s', v_credit),
                pg_catalog.clock_timestamp());
        RETURN v_credit;
    END
    $$
"""

CLOSE_V3 = """
    CREATE OR REPLACE FUNCTION public.assess_close_session(
        p_sid uuid, p_epoch integer, p_reason text
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
    BEGIN
        IF p_reason NOT IN ('finished', 'timed_out', 'service_failure', 'budget_exhausted', 'terminated_by_teacher', 'access_revoked') THEN
            RETURN false;
        END IF;
        SELECT s.state, s.epoch, s.site_id INTO v_state, v_epoch, v_site
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND THEN
            RETURN false;
        END IF;
        IF v_state = 'CLOSED' THEN
            RETURN true;  -- idempotent
        END IF;
        IF v_state NOT IN ('ACTIVE', 'OUTAGE') OR v_epoch <> p_epoch THEN
            RETURN false;
        END IF;
        -- an open outage interval ends with the close (no credit: the session is over)
        UPDATE public.assess_outage_incidents SET ended_at = pg_catalog.now()
         WHERE session_id = p_sid AND ended_at IS NULL;
        UPDATE public.assess_sessions
           SET state = 'CLOSED', close_reason = p_reason, closed_at = pg_catalog.now(), epoch = epoch + 1
         WHERE id = p_sid;
        INSERT INTO public.assess_jobs (kind, args, dedupe_key)
        VALUES ('transcript_generate',
                pg_catalog.jsonb_build_object('session_id', p_sid),
                'transcript_generate:' || p_sid::text);
        IF p_reason = 'service_failure' THEN
            -- REQ-086: the teacher hears of it within a minute (delivery: plan 10)
            INSERT INTO public.assess_jobs (kind, args, dedupe_key)
            VALUES ('teacher_notify',
                    pg_catalog.jsonb_build_object('session_id', p_sid, 'reason', p_reason),
                    'teacher_notify:' || p_sid::text);
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind)
        VALUES (v_site, p_sid, 'session_closed', p_reason,
                CASE WHEN session_user = 'vektra_assess_worker'
                     THEN 'worker' ELSE 'api' END);
        RETURN true;
    END
    $$
"""


def _previous_close() -> str:
    """``CLOSE_V2`` of revision 0025 (loaded from its file: revision modules are not a package)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("0025_assess_close_bumps_epoch.py")
    spec = importlib.util.spec_from_file_location("rev_0025_close", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.CLOSE_V2)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE assess_outage_incidents ADD COLUMN credited_s INTEGER NOT NULL DEFAULT 0 "
        "CONSTRAINT ck_assess_outage_incidents_credited CHECK (credited_s >= 0)"
    )
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind CHECK (kind IN ({JOB_KINDS}))"
    )
    op.execute(CLOSE_V3)
    op.execute(ENTER)
    op.execute(END)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema for the new owner (revoked again
    # by the rendered matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    for sig in (SIG_ENTER, SIG_END):
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIG_END}")
    op.execute(f"DROP FUNCTION {SIG_ENTER}")
    op.execute(_previous_close())
    op.execute("DELETE FROM assess_jobs WHERE kind = 'teacher_notify'")
    op.execute("ALTER TABLE assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(
        f"ALTER TABLE assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK (kind IN ({OLD_JOB_KINDS}))"
    )
    for stmt in (
        f"REVOKE ALL ON assess_outage_incidents FROM {DEFINER}",
        f"REVOKE USAGE ON SEQUENCE assess_outage_incidents_id_seq FROM {DEFINER}",
        f"REVOKE UPDATE (credited_outage_s, deadline_at) ON assess_sessions FROM {DEFINER}",
    ):
        op.execute(stmt)
    op.execute("ALTER TABLE assess_outage_incidents DROP COLUMN credited_s")
