"""The live course-module eligibility read of one RAG turn (FEAT-028, ARCH-072 amendment).

The API cannot ask the LMS: only the worker holds the provider's credential. A discussion turn
with course material therefore queues a ``material_eligibility`` job (args: the session and a
request id) and waits, a few seconds at most, for the worker's answer in this table:

- ``assess_material_reads``: one row per request (``id`` = the request id), bound to its session
  (``ON DELETE CASCADE``) and site; ``readable`` with the course ``namespace`` and the ELIGIBLE
  modules as ``[[cmid, title], ...]`` (titles of course modules, never content), or not readable
  with a closed ``reason`` code. ``read_at`` is the database clock of the write: the API accepts
  a row only for its own request and session, written after the turn began. The API deletes the
  row it read; the worker's sweeper deletes rows nobody picked up after five minutes;
- RLS for the API role, as ``a0002`` does on every site-scoped table it may read;
- ``ck_assess_jobs_kind`` gains ``material_eligibility``;
- ``GRANT_STATEMENTS``: today's rendering of ``vektra_assess.db.grants`` (API: SELECT, DELETE;
  worker: SELECT, INSERT, DELETE).

The downgrade removes the kind's jobs, the table and its policies, and puts back the grant matrix
of ``a0006``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "a0007"
down_revision = "a0006"
branch_labels = None
depends_on = None

API = "vektra_assess_api"
WORKER = "vektra_assess_worker"
DEFINER = "vektra_assess_definer"
TABLE = "assess_material_reads"
BOUND_SITE = "NULLIF(pg_catalog.current_setting('app.assess_site_id', true), '')::uuid"

KINDS_BEFORE = "ARRAY['setup_check'::text, 'gate_refresh'::text, 'render_check'::text, 'transcript_generate'::text, 'transcript_deliver'::text, 'enrolment_recheck'::text, 'purge'::text, 'erasure'::text, 'preflight'::text, 'reconcile_reply'::text, 'teacher_notify'::text, 'operator_notify'::text, 'teacher_gate'::text, 'smtp_test_send'::text]"
KINDS_AFTER = "ARRAY['setup_check'::text, 'gate_refresh'::text, 'render_check'::text, 'transcript_generate'::text, 'transcript_deliver'::text, 'enrolment_recheck'::text, 'purge'::text, 'erasure'::text, 'preflight'::text, 'reconcile_reply'::text, 'teacher_notify'::text, 'operator_notify'::text, 'teacher_gate'::text, 'smtp_test_send'::text, 'material_eligibility'::text]"

CREATE = r"""
CREATE TABLE public.assess_material_reads (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    site_id uuid NOT NULL,
    readable boolean NOT NULL,
    reason text,
    namespace text,
    modules jsonb,
    read_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT assess_material_reads_pkey PRIMARY KEY (id),
    CONSTRAINT assess_material_reads_session_id_fkey FOREIGN KEY (session_id)
        REFERENCES public.assess_sessions(id) ON DELETE CASCADE,
    CONSTRAINT assess_material_reads_site_id_fkey FOREIGN KEY (site_id)
        REFERENCES public.assess_sites(id) ON DELETE CASCADE,
    CONSTRAINT ck_assess_material_reads_shape CHECK ((
        (readable AND namespace IS NOT NULL AND modules IS NOT NULL AND reason IS NULL)
        OR (NOT readable AND namespace IS NULL AND modules IS NULL AND reason IS NOT NULL)
    )),
    CONSTRAINT ck_assess_material_reads_reason CHECK (((reason IS NULL) OR (reason ~ '^[a-z_]{1,64}$')))
)
"""

POLICIES = (
    f"ALTER TABLE public.{TABLE} ENABLE ROW LEVEL SECURITY",
    f"CREATE POLICY assess_api_site_bound ON public.{TABLE} AS PERMISSIVE FOR ALL "
    f"TO {API} USING (site_id = {BOUND_SITE}) WITH CHECK (site_id = {BOUND_SITE})",
    f"CREATE POLICY assess_unbound_roles ON public.{TABLE} AS PERMISSIVE FOR ALL "
    f"TO {WORKER}, {DEFINER} USING (true) WITH CHECK (true)",
)

#: the rendering of ``vektra_assess.db.grants`` at this revision (the unit test compares)
GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_activity_config_sealed, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_material_reads, assess_model_activations, assess_outage_incidents, assess_privacy_config, assess_purge_records, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_teacher_gate, assess_terminations, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_purge_records_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE CREATE ON SCHEMA public FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON query_traces FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "GRANT INSERT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by, rag_attested_at, rag_attested_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT SELECT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by, rag_attested_at, rag_attested_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT UPDATE (enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by, rag_attested_at, rag_attested_by) ON assess_activity_config TO vektra_assess_api",
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
    "GRANT SELECT ON assess_erasure_records TO vektra_assess_api",
    "GRANT INSERT ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_api",
    "GRANT UPDATE ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_api",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_api",
    "GRANT INSERT ON assess_jobs TO vektra_assess_api",
    "GRANT DELETE ON assess_material_reads TO vektra_assess_api",
    "GRANT SELECT ON assess_material_reads TO vektra_assess_api",
    "GRANT INSERT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_api",
    "GRANT INSERT ON assess_privacy_config TO vektra_assess_api",
    "GRANT SELECT ON assess_privacy_config TO vektra_assess_api",
    "GRANT UPDATE (retention_days, legal_basis_label, legal_basis_text, notice_url, notice_text, notice_version, updated_by, updated_at) ON assess_privacy_config TO vektra_assess_api",
    "GRANT SELECT ON assess_purge_records TO vektra_assess_api",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_checks TO vektra_assess_api",
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
    "GRANT SELECT (id, lms_site_uuid, base_url, release, min_plugin_version, created_at, disabled_at, last_seen_release, lms_origin) ON assess_sites TO vektra_assess_api",
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
    "GRANT DELETE ON assess_extra_time TO vektra_assess_worker",
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
    "GRANT DELETE ON assess_material_reads TO vektra_assess_worker",
    "GRANT INSERT ON assess_material_reads TO vektra_assess_worker",
    "GRANT SELECT ON assess_material_reads TO vektra_assess_worker",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_worker",
    "GRANT DELETE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT UPDATE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT SELECT ON assess_privacy_config TO vektra_assess_worker",
    "GRANT INSERT ON assess_purge_records TO vektra_assess_worker",
    "GRANT SELECT ON assess_purge_records TO vektra_assess_worker",
    "GRANT UPDATE (subject_ref) ON assess_purge_records TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_checks TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_state TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_state TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_state TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_state TO vektra_assess_worker",
    "GRANT DELETE ON assess_reset_grants TO vektra_assess_worker",
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
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_purge_records_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_definer",
    "GRANT INSERT ON assess_deliveries TO vektra_assess_definer",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_definer",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_definer",
    "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
    "GRANT SELECT ON assess_jobs TO vektra_assess_definer",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT UPDATE (ended_at, credited_s) ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT SELECT (site_id, notice_url) ON assess_privacy_config TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT (session_id) ON assess_session_keys TO vektra_assess_definer",
    "GRANT DELETE ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since, epoch, credited_outage_s, deadline_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT (site_id, api_key_id, purpose) ON assess_site_keys TO vektra_assess_definer",
    "GRANT SELECT (id, min_plugin_version, disabled_at) ON assess_sites TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_definer",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_definer",
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
    "REVOKE ALL ON FUNCTION public.assess_configured_site_count() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_configured_site_count() TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_end_outage(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_end_outage(uuid,integer) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_live_session_count() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_live_session_count() TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_metrics_snapshot() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_metrics_snapshot() TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_pending_replies(double precision,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_pending_replies(double precision,integer) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_rollback_starting(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_rollback_starting(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_session_exists(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_session_exists(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_site_of_key(uuid,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_site_of_key(uuid,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_transcript_job_states(uuid[]) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_transcript_job_states(uuid[]) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_transcript_site(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_transcript_site(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)


def _previous_grants() -> tuple[str, ...]:
    """The matrix of ``a0006`` (the last revision that carried one before this)."""
    path = Path(__file__).with_name("a0006_assess_rag_attestation.py")
    spec = importlib.util.spec_from_file_location("assess_a0006_for_downgrade", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.GRANT_STATEMENTS)


def _kinds(array: str) -> str:
    return (
        "ALTER TABLE public.assess_jobs ADD CONSTRAINT ck_assess_jobs_kind "
        f"CHECK ((kind = ANY ({array})))"
    )


def upgrade() -> None:
    op.execute(CREATE)
    op.execute(f"CREATE INDEX ix_{TABLE}_read_at ON public.{TABLE} (read_at)")
    op.execute(f"CREATE INDEX ix_{TABLE}_session ON public.{TABLE} (session_id)")
    for stmt in POLICIES:
        op.execute(stmt)
    op.execute("ALTER TABLE public.assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(_kinds(KINDS_AFTER))
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    # the previous matrix names no privilege on the table dropped below
    for stmt in _previous_grants():
        op.execute(stmt)
    op.execute("DELETE FROM public.assess_jobs WHERE kind = 'material_eligibility'")
    op.execute("ALTER TABLE public.assess_jobs DROP CONSTRAINT ck_assess_jobs_kind")
    op.execute(_kinds(KINDS_BEFORE))
    op.execute(f"DROP TABLE public.{TABLE}")
