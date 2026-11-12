"""Row-level security for the API role on the site-scoped tables (ADR-0033 D9.2, plan 3.2).

Every ``assess:*`` key is bound to exactly one site, and every student token names one. The API
sets ``app.assess_site_id`` at the start of each request transaction
(``vektra_assess.api.deps``); this revision makes the database hold the API role to it, so a
route that forgets its ``site_id`` predicate reads nothing of another site instead of reading it.

What it does, on the 20 tables that have a ``site_id`` column and that ``vektra_assess_api`` may
read (:data:`SITE_TABLES`):

- ``ENABLE ROW LEVEL SECURITY``, not ``FORCE``: the tables are owned by the migration user,
  which must keep every row (a later data migration, the base revision's own downgrade check
  on ``assess_sessions``). ``FORCE`` with no policy for the owner would hide every row from it;
- policy ``assess_api_site_bound``, ``TO vektra_assess_api``, for every command, ``USING`` and
  ``WITH CHECK``: ``site_id = NULLIF(current_setting('app.assess_site_id', true), '')::uuid``.
  Unset (NULL) and empty (what a transaction-local setting leaves on a pooled connection after
  its transaction) both give NULL, which matches no row: the API role reads nothing and writes
  nothing until it is bound. A malformed value is a cast error (``22P02``), never a wider read;
  the API only ever binds a ``uuid`` it has validated. The predicate is a stable expression on
  the indexed ``site_id``, so it is an index condition;
- policy ``assess_unbound_roles``, ``TO vektra_assess_worker, vektra_assess_definer``, ``USING
  (true)``: the worker is multi-site by construction (D9.2 keeps it without RLS), and a
  SECURITY DEFINER function runs as ``vektra_assess_definer``, which owns no table and has no
  BYPASSRLS, so without its own policy every definer function would see no row once RLS is on.

Left out, with the reason:

- ``assess_audit_events``: the API may only INSERT into it (no SELECT, nothing to read across
  sites), and some of its rows are refusals written before any site is trusted (a token that
  failed verification: ``site_id`` NULL). A ``WITH CHECK`` there would turn those audits into
  errors;
- ``assess_setup_checks``: the API has no privilege on it;
- ``assess_sites``: the site itself (no ``site_id``), read by the UI origin check at startup
  across every site; it holds no student data;
- the tables without a ``site_id`` (turns, snapshots, transcripts, deliveries, ...): the API
  reaches them through a session row or a SECURITY DEFINER function, never by themselves (the
  column grants, D9 layer 1, still keep turns, snapshots and keys away from the API role).

The reads that legitimately precede the binding or span sites move to five SECURITY DEFINER
functions owned by ``vektra_assess_definer`` (search path pinned, never PUBLIC, EXECUTE for the
API only), each returning ids or counts: :data:`FUNCTIONS`. The grant matrix is carried again
(:data:`GRANT_STATEMENTS`, rendered by ``vektra_assess.db.grants.render_statements``) with the
definer's three new column grants.

The downgrade drops the policies, disables RLS, drops the five functions and puts back the grant
matrix of ``a0001``.

Revision ID: a0002
Revises: a0001
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "a0002"
down_revision = "a0001"
branch_labels = None
depends_on = None

API = "vektra_assess_api"
WORKER = "vektra_assess_worker"
DEFINER = "vektra_assess_definer"

#: the transaction-local setting the API binds (``vektra_assess.api.deps.SITE_SETTING``)
SITE_SETTING = "app.assess_site_id"
#: the site a transaction is bound to; NULL when unset or empty
BOUND_SITE = f"NULLIF(pg_catalog.current_setting('{SITE_SETTING}', true), '')::uuid"

API_POLICY = "assess_api_site_bound"
TRUSTED_POLICY = "assess_unbound_roles"

#: every table with a ``site_id`` column that the API role may SELECT; the integration test
#: ``test_api_rls.py`` derives the same set from the catalog and compares
SITE_TABLES: tuple[str, ...] = (
    "assess_activity_config",
    "assess_activity_config_sealed",
    "assess_erasure_records",
    "assess_extra_time",
    "assess_gate_inputs",
    "assess_privacy_config",
    "assess_purge_records",
    "assess_render_checks",
    "assess_render_state",
    "assess_reset_grants",
    "assess_sessions",
    "assess_setup_check_reports",
    "assess_site_capabilities",
    "assess_site_keys",
    "assess_start_attempts",
    "assess_start_breaker",
    "assess_teacher_gate",
    "assess_terminations",
    "assess_token_jti",
    "assess_transcript_download_tokens",
)

DEFINER_MEMBERSHIP_MESSAGE = (
    "assess revision a0002: the migration user cannot SET ROLE vektra_assess_definer, which "
    "owns the SECURITY DEFINER functions (ALTER FUNCTION ... OWNER TO needs it). "
    "An administrator runs GRANT vektra_assess_definer TO <migration user>; "
    "then re-run the migration. Nothing was changed."
)

FUNCTIONS: tuple[str, ...] = (
    # the key -> site resolution: it is what names the site, so it runs before any binding.
    # ``disabled`` is returned, not filtered: the setup check refuses a disabled site's key
    # with its own 404, the other routes with ERR-ASSESS-007
    r"""
CREATE FUNCTION public.assess_site_of_key(p_key_id uuid, p_purpose text) RETURNS TABLE(id uuid, min_plugin_version text, disabled boolean)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT s.id, s.min_plugin_version, s.disabled_at IS NOT NULL
        FROM public.assess_site_keys k
        JOIN public.assess_sites s ON s.id = k.site_id
        WHERE k.api_key_id = p_key_id AND k.purpose = p_purpose
    $$
    """,
    # the deployment-wide admission cap counts live sessions of every site (REQ-071)
    r"""
CREATE FUNCTION public.assess_live_session_count() RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT count(*) FROM public.assess_sessions WHERE state <> 'CLOSED'
    $$
    """,
    # /health/assess: enabled sites whose privacy notice URL names a host (plan 12b)
    r"""
CREATE FUNCTION public.assess_configured_site_count() RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT count(*) FROM public.assess_privacy_config p
        JOIN public.assess_sites s ON s.id = p.site_id
        WHERE s.disabled_at IS NULL AND p.notice_url ~ '^https://[^/?#@\s\[\]]+'
    $$
    """,
    # the PendingTurnDriver's scan (REQ-086): ACTIVE sessions whose student turn has waited at
    # least p_min_age_s, inside deadline + grace, that no reply holds the live-reply claim of
    # (``vektra_assess.turn_markers.reply_claimed_sql``, the same text); oldest first
    r"""
CREATE FUNCTION public.assess_pending_replies(p_min_age_s double precision, p_limit integer) RETURNS TABLE(id uuid, site_id uuid)
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT s.id, s.site_id FROM public.assess_sessions s
        WHERE s.state = 'ACTIVE'
          AND s.pending_reply_since IS NOT NULL
          AND s.pending_reply_since <= now() - make_interval(secs => p_min_age_s)
          AND s.deadline_at + make_interval(secs => s.reply_grace_s) > now()
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks l WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 AND ((l.classid::bigint << 32) | l.objid::bigint) = hashtextextended('assess_reply:' || CAST(s.id AS text), 0))
        ORDER BY s.pending_reply_since
        LIMIT p_limit
    $$
    """,
    # an operator's resend names a transcript, not a site: the site to bind first
    r"""
CREATE FUNCTION public.assess_transcript_site(p_tid uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT s.site_id FROM public.assess_transcripts t
        JOIN public.assess_sessions s ON s.id = t.session_id
        WHERE t.id = p_tid
    $$
    """,
)

DEFINER_OWNED: tuple[str, ...] = (
    "public.assess_site_of_key(p_key_id uuid, p_purpose text)",
    "public.assess_live_session_count()",
    "public.assess_configured_site_count()",
    "public.assess_pending_replies(p_min_age_s double precision, p_limit integer)",
    "public.assess_transcript_site(p_tid uuid)",
)


def policy_statements() -> list[str]:
    out = []
    for table in SITE_TABLES:
        out.append(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        out.append(
            f"CREATE POLICY {API_POLICY} ON public.{table} AS PERMISSIVE FOR ALL "
            f"TO {API} USING (site_id = {BOUND_SITE}) WITH CHECK (site_id = {BOUND_SITE})"
        )
        out.append(
            f"CREATE POLICY {TRUSTED_POLICY} ON public.{table} AS PERMISSIVE FOR ALL "
            f"TO {WORKER}, {DEFINER} USING (true) WITH CHECK (true)"
        )
    return out


#: the grant matrix, rendered by ``vektra_assess.db.grants.render_statements()`` (a migration does
#: not import application code at run time; ``test_grant_matrix.py`` asserts the two are equal)
GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_activity_config_sealed, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_privacy_config, assess_purge_records, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_teacher_gate, assess_terminations, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_purge_records_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
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
    "GRANT SELECT ON assess_erasure_records TO vektra_assess_api",
    "GRANT INSERT ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_api",
    "GRANT UPDATE ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_api",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_api",
    "GRANT INSERT ON assess_jobs TO vektra_assess_api",
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
    "REVOKE ALL ON FUNCTION public.assess_site_of_key(uuid,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_site_of_key(uuid,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_transcript_site(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_transcript_site(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)


def _base_revision_grants() -> tuple[str, ...]:
    """The matrix of ``a0001``, read from its file (the revisions are not a package)."""
    path = Path(__file__).with_name("a0001_assess_base.py")
    spec = importlib.util.spec_from_file_location("assess_a0001_for_downgrade", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.GRANT_STATEMENTS)


def upgrade() -> None:
    can_set = (
        op.get_bind()
        .execute(sa.text("SELECT pg_has_role(current_user, :r, 'SET')"), {"r": DEFINER})
        .scalar()
    )
    if not can_set:
        raise RuntimeError(DEFINER_MEMBERSHIP_MESSAGE)
    for stmt in FUNCTIONS:
        op.execute(stmt)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema for the new owner (unless the
    # migration user is a superuser); the matrix below revokes it again
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    for sig in DEFINER_OWNED:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER}")
    for stmt in policy_statements():
        op.execute(stmt)
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in SITE_TABLES:
        op.execute(f"DROP POLICY {TRUSTED_POLICY} ON public.{table}")
        op.execute(f"DROP POLICY {API_POLICY} ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
    for sig in DEFINER_OWNED:
        op.execute(f"DROP FUNCTION {sig}")
    for stmt in _base_revision_grants():
        op.execute(stmt)
