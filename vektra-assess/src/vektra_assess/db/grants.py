"""The assess grant matrix as data (INT-002, plan assess-data-crypto 3.1).

This module is the single description of who may touch what. Three things derive from it:

- the SQL of revision ``0015`` (rendered once and committed in the revision, which does not
  import application code at run time; a test asserts the committed text equals
  ``render_statements()``);
- the catalog test that compares ``information_schema`` / ``pg_proc`` to it in BOTH
  directions (no missing grant, no extra grant);
- the behavioural test that connects as each role and observes ``permission denied``.

A privilege maps to ``None`` (whole table) or to a tuple of columns (column-level).
A table absent from a role's entry means NO privilege at all.
"""

from __future__ import annotations

from collections.abc import Mapping

API = "vektra_assess_api"
WORKER = "vektra_assess_worker"
DEFINER = "vektra_assess_definer"
ROLES = (API, WORKER, DEFINER)

Privs = Mapping[str, tuple[str, ...] | None]

#: every assess_* table; a test fails if the database has one that is not listed here.
TABLES: tuple[str, ...] = (
    "assess_activity_config",
    "assess_activity_config_sealed",  # revision 0036 (plan 11): one sealed row per field
    "assess_audit_events",
    "assess_consumed_jtis",
    "assess_deliveries",
    "assess_enrolment_checks",
    "assess_erasure_records",
    "assess_extra_time",
    "assess_gate_inputs",
    "assess_heartbeat",
    "assess_jobs",
    "assess_model_activations",
    "assess_outage_incidents",
    "assess_privacy_config",  # revision 0040 (plan 12): the REQ-089 settings of a site
    "assess_purge_records",  # revision 0040 (plan 12): the purge tombstones, ids and counts only
    "assess_render_checks",
    "assess_render_state",
    "assess_reset_grants",
    "assess_session_handoff",
    "assess_session_keys",
    "assess_sessions",
    "assess_setup_check_reports",
    "assess_setup_checks",
    "assess_site_capabilities",
    "assess_site_keys",
    "assess_sites",
    "assess_start_breaker",
    "assess_snapshots",
    "assess_start_attempts",
    "assess_teacher_gate",  # revision 0036 (plan 11): worker-written activity-level facts
    "assess_terminations",  # revision 0038 (plan 11b): one row per teacher termination
    "assess_token_jti",
    "assess_transcript_download_tokens",
    "assess_transcripts",
    "assess_turn_flags",
    "assess_turn_notes",
    "assess_turns",
)

#: BIGSERIAL sequences: INSERT needs USAGE on them.
SEQUENCES: tuple[str, ...] = (
    "assess_audit_events_id_seq",
    "assess_enrolment_checks_id_seq",
    "assess_erasure_records_id_seq",
    "assess_jobs_id_seq",
    "assess_model_activations_id_seq",
    "assess_outage_incidents_id_seq",
    "assess_purge_records_id_seq",
    "assess_render_checks_id_seq",
    "assess_setup_checks_id_seq",
    "assess_start_attempts_id_seq",
)

#: columns of assess_activity_config the API may read and write (revision 0036: the sealed
#: teacher config moved to assess_activity_config_sealed, so nothing here is secret any more;
#: the list stays explicit so a future secret column is never readable by default)
_CONFIG_READ = (
    "id",
    "site_id",
    "cmid",
    "activity_type",
    "enabled",
    "flags",
    "rag_enabled",
    "end_at",
    "attested_opening_at",
    "attested_by",
    "updated_at",
    "cap_minutes",
    "version",
    "updated_by",
)
_CONFIG_WRITE = _CONFIG_READ
#: columns the API may UPDATE on an existing config row (never its identity columns)
_CONFIG_UPDATE = tuple(
    c for c in _CONFIG_WRITE if c not in ("id", "site_id", "cmid", "activity_type")
)
#: assess_activity_config_sealed (revision 0036, ARCH-071 / OQ-034): the API seals and writes a
#: field, it may read only WHICH fields are set (and how many recipients), never ``sealed_blob``
#: nor ``refs`` (the recipient refs, hashes of guessable addresses: REQ-082 as amended). Clearing
#: a field is an UPDATE to a NULL blob, so the API has no DELETE.
_SEALED_API_READ = (
    "site_id", "cmid", "field", "is_set", "item_count", "updated_by", "updated_at",
)  # fmt: skip
_SEALED_API_INSERT = (
    "site_id", "cmid", "field", "sealed_blob", "kid", "refs", "item_count", "updated_by",
    "updated_at",
)  # fmt: skip
_SEALED_API_UPDATE = (
    "sealed_blob", "kid", "refs", "item_count", "updated_by", "updated_at",
)  # fmt: skip
_SITES_API_READ = (
    "id", "lms_site_uuid", "base_url", "release", "min_plugin_version", "created_at",
    "disabled_at", "last_seen_release",
    "lms_origin",  # revision 0033 (plan 9): the one origin allowed to frame the UI
)  # fmt: skip
#: columns of assess_deliveries the API may read: the status, never the recipient ref nor the
#: Message-ID, which hashes the ref too (0035)
_DELIVERY_API_READ = (
    "id", "transcript_id", "kind", "round", "recipient_kind", "recipient_userid", "status",
    "attempts", "not_before", "attempt_started_at", "last_error_class", "smtp_code",
    "sent_at", "created_at",
)  # fmt: skip
#: columns of assess_privacy_config the API may UPDATE (never the site it belongs to)
_PRIVACY_UPDATE = (
    "retention_days", "legal_basis_label", "legal_basis_text", "notice_url", "notice_text",
    "notice_version", "updated_by", "updated_at",
)  # fmt: skip
_ALL: None = None

MATRIX: Mapping[str, Mapping[str, Privs]] = {
    API: {
        "assess_jobs": {"INSERT": _ALL},
        # NFR-023 (plan assess-llm-runner): the admin endpoint appends, startup reads; append-only
        "assess_model_activations": {"SELECT": _ALL, "INSERT": _ALL},
        # SELECT since revision 0040: the privacy listing names every subject with a row
        "assess_render_checks": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_render_state": {"SELECT": _ALL},
        "assess_gate_inputs": {"SELECT": _ALL},
        "assess_enrolment_checks": {"SELECT": _ALL},
        # the outage cap C of the lazy close (plan 8 M3.4): read only
        "assess_outage_incidents": {"SELECT": _ALL},
        "assess_heartbeat": {"SELECT": _ALL},
        # not probe_manifest: the fixture expectations are the worker's
        "assess_sites": {"SELECT": _SITES_API_READ},
        "assess_site_keys": {"SELECT": _ALL},
        "assess_site_capabilities": {"SELECT": _ALL},
        # the API queues a setup check (INSERT) and reads its content-free report
        "assess_setup_check_reports": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_sessions": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            # seq and last_activity_at move only via the functions; tokens_used is the REQ-083
            # session budget the TurnDriver adds each answered turn's TurnUsage to (rev 0027)
            "UPDATE": ("epoch", "tokens_used"),
        },
        # SELECT: the Start cap and the rollback reason of a STARTING session (plan 8)
        "assess_start_attempts": {"SELECT": _ALL, "INSERT": _ALL},
        # the per-site Start breaker: written by the worker only, read for the fast refusal
        "assess_start_breaker": {"SELECT": _ALL},
        # REQ-103 flags (revision 0030): one marker per bot turn, never updated or deleted here
        "assess_turn_flags": {"SELECT": _ALL, "INSERT": _ALL},
        # mint inserts, Start / resume consume (plan 8); identity columns never change
        "assess_token_jti": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("consumed_at",),
        },
        "assess_audit_events": {"INSERT": _ALL},
        # revision 0040 (plan 12): the erasure endpoint queues a record and waits for the
        # worker's completion on it (the API deletes nothing itself)
        "assess_erasure_records": {"SELECT": _ALL, "INSERT": _ALL},
        # revision 0040 (plan 12, REQ-089): the operator writes a site's privacy settings
        "assess_privacy_config": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _PRIVACY_UPDATE,
        },
        # revision 0040: the tombstones answer 410 and ``transcripts[].purged``
        "assess_purge_records": {"SELECT": _ALL},
        "assess_activity_config": {
            "SELECT": _CONFIG_READ,
            "INSERT": _CONFIG_WRITE,
            "UPDATE": _CONFIG_UPDATE,
        },
        "assess_activity_config_sealed": {
            "SELECT": _SEALED_API_READ,
            "INSERT": _SEALED_API_INSERT,
            "UPDATE": _SEALED_API_UPDATE,
        },
        # the teacher endpoints verify the actor against these fresh facts (plan 11)
        "assess_teacher_gate": {"SELECT": _ALL},
        "assess_extra_time": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL},
        # revision 0038 (plan 11b): a reset is granted once and never rewritten (its reason and
        # continuity choice are what the teacher signed): no UPDATE
        "assess_reset_grants": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_terminations": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_consumed_jtis": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("consumed_at",),
        },
        "assess_transcript_download_tokens": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("consumed_at",),
        },
        "assess_transcripts": {"SELECT": _ALL},
        # read for the delivery status; resend rounds go through assess_enqueue_resend (0034).
        # Revision 0035: never ``recipient_ref`` (a hash of an address the API must not be able
        # to confirm by guessing, REQ-082 as amended)
        "assess_deliveries": {"SELECT": _DELIVERY_API_READ},
        # revision 0034 (plan 10): the post-check marks and contest entries of a bot turn,
        # written in the turn's own transaction, never updated
        "assess_turn_notes": {"INSERT": _ALL},
        # snapshots, turns, session_keys, session_handoff: no privilege at all
    },
    WORKER: {
        # plumbing and status
        "assess_jobs": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL, "DELETE": _ALL},
        # the Part B pre-flight refuses an unactivated triple (plan 8): read only
        "assess_model_activations": {"SELECT": _ALL},
        "assess_render_checks": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_render_state": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_gate_inputs": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_heartbeat": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_setup_checks": {"SELECT": _ALL, "INSERT": _ALL, "DELETE": _ALL},
        "assess_enrolment_checks": {"SELECT": _ALL, "INSERT": _ALL, "DELETE": _ALL},
        "assess_outage_incidents": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_start_attempts": {"SELECT": _ALL, "INSERT": _ALL, "DELETE": _ALL},
        "assess_start_breaker": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_consumed_jtis": {"SELECT": _ALL, "DELETE": _ALL},
        # the done-job cleanup tick deletes expired mint rows (plan 8)
        "assess_token_jti": {"SELECT": _ALL, "DELETE": _ALL},
        "assess_transcript_download_tokens": {"SELECT": _ALL, "DELETE": _ALL},
        # registry and configuration (the worker reads the sealed teacher config)
        "assess_sites": {
            "SELECT": _ALL,
            "UPDATE": (
                "release",
                "min_plugin_version",
                "last_seen_release",
                "probe_manifest",
            ),
        },
        "assess_setup_check_reports": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_site_keys": {"SELECT": _ALL},
        "assess_site_capabilities": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_activity_config": {"SELECT": _ALL},
        # opens the sealed fields at capture and delivery; the rotation re-seals blob and kid
        "assess_activity_config_sealed": {
            "SELECT": _ALL,
            "UPDATE": ("sealed_blob", "kid"),
        },
        # the teacher_gate job writes the activity-level facts (editors, graders, enable facts)
        "assess_teacher_gate": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        # revision 0040 (plan 12): the erasure deletes the subject's grants
        "assess_extra_time": {"SELECT": _ALL, "DELETE": _ALL},
        # revision 0038: the transcript-KEK rotation re-encrypts the teacher's reasons;
        # revision 0040: purge and erasure delete the grants of the sessions they remove
        "assess_reset_grants": {
            "SELECT": _ALL,
            "UPDATE": ("consumed_at", "reason_ct", "reason_kid"),
            "DELETE": _ALL,
        },
        # revision 0040 (plan 12): retention per site, the tombstones the purge writes
        "assess_privacy_config": {"SELECT": _ALL},
        # an erasure makes earlier tombstones of its subject anonymous (subject_ref only)
        "assess_purge_records": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("subject_ref",),
        },
        "assess_terminations": {"SELECT": _ALL, "UPDATE": ("reason_ct", "reason_kid")},
        # session data. Snapshots and turns are never UPDATEd (BR-015); the worker reads
        # them to build the opening turn and the transcript and deletes them on purge.
        "assess_sessions": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_snapshots": {"SELECT": _ALL, "INSERT": _ALL, "DELETE": _ALL},
        "assess_turns": {"SELECT": _ALL, "INSERT": _ALL, "DELETE": _ALL},
        "assess_turn_flags": {
            "SELECT": _ALL,
            "DELETE": _ALL,
        },  # transcript reads, purge deletes
        "assess_turn_notes": {
            "SELECT": _ALL,
            "DELETE": _ALL,
        },  # revision 0034, same rule
        "assess_session_keys": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_session_handoff": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_transcripts": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_deliveries": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_erasure_records": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL},
        "assess_audit_events": {"INSERT": _ALL},
    },
    DEFINER: {
        "assess_sessions": {
            "SELECT": _ALL,
            # revision 0038 (plan 11b): assess_rollback_starting deletes a STARTING row only
            "DELETE": _ALL,
            "UPDATE": (
                "seq",
                "last_activity_at",
                "state",
                "close_reason",
                "closed_at",
                "pending_reply_since",  # revision 0023 (plan 8): the one-reply-at-a-time rule
                "epoch",  # revision 0025: every close bumps the epoch (revokes session tokens)
                # revision 0026 (plan 8 M4): the outage functions credit the outage and move the
                # deadline by the one formula
                "credited_outage_s",
                "deadline_at",
            ),
        },
        # revision 0026: ACTIVE -> OUTAGE records the interval, its end sets ended_at + credit
        "assess_outage_incidents": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("ended_at", "credited_s"),
        },
        "assess_session_handoff": {"SELECT": _ALL, "DELETE": _ALL},
        # revision 0038: assess_rollback_starting counts a STARTING session's key rows (the id
        # only: never the wrapped key)
        "assess_session_keys": {"SELECT": ("session_id",)},
        "assess_snapshots": {"SELECT": _ALL},
        "assess_turns": {"SELECT": _ALL, "INSERT": _ALL},
        # SELECT since revision 0041 (plan 12b): assess_metrics_snapshot() counts ready jobs and
        # rollbacks; it returns counts only (the audit table stays insert-only for everyone)
        "assess_jobs": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_audit_events": {"INSERT": _ALL},
        # revision 0038: the teacher's rollback of a STARTING session records its attempt
        "assess_start_attempts": {"SELECT": _ALL, "INSERT": _ALL},
        # revision 0041: the snapshot reads the worker, smtp and purge rows
        "assess_heartbeat": {"SELECT": _ALL},
        # revision 0034 (plan 10): assess_enqueue_resend reads the transcript and the last
        # round, and inserts round n+1; it never updates or deletes a delivery
        "assess_transcripts": {"SELECT": _ALL},
        "assess_deliveries": {"SELECT": _ALL, "INSERT": _ALL},
        # revision a0002 (ADR-0033 D9.2, RLS for the API role): the API reads its key's site
        # (assess_site_of_key) and the deployment-wide health count (assess_configured_site_count)
        # through the definer, column by column, never a whole row
        "assess_site_keys": {"SELECT": ("site_id", "api_key_id", "purpose")},
        "assess_sites": {"SELECT": ("id", "min_plugin_version", "disabled_at")},
        "assess_privacy_config": {"SELECT": ("site_id", "notice_url")},
    },
}

#: sequences each role may USE (BIGSERIAL inserts).
SEQUENCE_USAGE: Mapping[str, tuple[str, ...]] = {
    API: (
        "assess_audit_events_id_seq",
        "assess_erasure_records_id_seq",
        "assess_jobs_id_seq",
        "assess_model_activations_id_seq",
        "assess_render_checks_id_seq",
        "assess_start_attempts_id_seq",
    ),
    WORKER: SEQUENCES,
    DEFINER: (
        "assess_audit_events_id_seq",
        "assess_jobs_id_seq",
        "assess_outage_incidents_id_seq",  # revision 0026
        "assess_start_attempts_id_seq",  # revision 0038
    ),
}

#: privileges on tables OUTSIDE ``assess_*`` (every other table: none at all, checked by the
#: catalog test). The worker writes the content-free QueryTrace of Part B (plan assess-llm-runner,
#: revision 0020): INSERT only, no SELECT, and a RESTRICTIVE RLS policy limits its rows to the
#: reserved ``assess-discussion`` namespace and pipeline.
QUERY_TRACE_COLUMNS = (
    "response_id",
    "namespace_id",
    "steps",
    "total_duration_ms",
    "chunks_retrieved",
    "llm_model",
    "prompt_version",
    "created_at",
    "pipeline",
)
EXTERNAL: Mapping[str, Mapping[str, Privs]] = {
    API: {},
    WORKER: {"query_traces": {"INSERT": QUERY_TRACE_COLUMNS}},
    DEFINER: {},
}
EXTERNAL_TABLES: tuple[str, ...] = ("query_traces",)

#: SECURITY DEFINER functions (signature -> roles with EXECUTE). Never PUBLIC.
FUNCTIONS: Mapping[str, tuple[str, ...]] = {
    "get_session_material(uuid,integer)": (API,),
    # the worker's reconcile_reply appends the marked interrupted turn (revision 0024)
    "assess_append_turn(uuid,integer,integer,text,text,bytea,text,text)": (API, WORKER),
    "assess_close_session(uuid,integer,text)": (API, WORKER),
    # REQ-086 (revision 0026): the API's turn drivers move a session into OUTAGE, the worker's
    # outage probe ends it with the credit
    "assess_enter_outage(uuid,integer,timestamp with time zone,text)": (API,),
    "assess_end_outage(uuid,integer)": (WORKER,),
    # REQ-082 resend (revision 0034): a new round for the transcript's current recipients; the
    # caller can never name a recipient
    "assess_enqueue_resend(uuid,text,integer)": (API,),
    # revision 0038 (plan 11b, REQ-081): a teacher's terminate rolls a STARTING session back to
    # ELIGIBLE (deleted, nothing consumed); it refuses any other state
    "assess_rollback_starting(uuid)": (API,),
    # revision 0036 (plan 11): the trigger that audits a sealed teacher-config change old/new
    # from the row itself (the API cannot read the old recipient refs). Nobody EXECUTEs it.
    "assess_audit_sealed_change()": (),
    # revision 0041 (plan 12b): the content-free counts behind the API's /metrics collector
    "assess_metrics_snapshot()": (API,),
    # revision a0002 (ADR-0033 D9.2): the API's reads that need no site binding or span every
    # site, now that its own reads of the site-scoped tables are bound by RLS. Each returns ids
    # or counts, never content.
    # the key -> site resolution itself (it precedes the binding)
    "assess_site_of_key(uuid,text)": (API,),
    # the deployment-wide live-session admission cap (REQ-071, start.py)
    "assess_live_session_count()": (API,),
    # /health/assess: sites with a complete privacy notice, over every site
    "assess_configured_site_count()": (API,),
    # the PendingTurnDriver's scan over every site: (session, site) to bind each answer to
    "assess_pending_replies(double precision,integer)": (API,),
    # an operator's (admin key) transcript resend: the site to bind before reading the row
    "assess_transcript_site(uuid)": (API,),
    # the session gate: whether a sid the bound read cannot see exists in another site (the
    # ownership-mismatch audit), one boolean
    "assess_session_exists(uuid)": (API,),
    # revision a0004 (#215): the subject export says why a closed session's turns are missing
    # (its transcript job pending or failed); the API reads no job row itself
    "assess_transcript_job_states(uuid[])": (API,),
}


def render_statements() -> list[str]:
    """The SQL of the latest grants revision, in order: revoke everything, then grant the
    matrix, then the SECURITY DEFINER functions' EXECUTE (rendered from revision 0024 on)."""
    out: list[str] = []
    tables = ", ".join(TABLES)
    roles = ", ".join(ROLES)
    out.append(f"REVOKE ALL ON {tables} FROM {roles}")
    out.append(f"REVOKE ALL ON SEQUENCE {', '.join(SEQUENCES)} FROM {roles}")
    out.append(f"REVOKE CREATE ON SCHEMA public FROM {roles}")
    out.append(f"REVOKE ALL ON {', '.join(EXTERNAL_TABLES)} FROM {roles}")
    for role in ROLES:
        for table in sorted(MATRIX[role]):
            for priv, cols in sorted(MATRIX[role][table].items()):
                col = f" ({', '.join(cols)})" if cols else ""
                out.append(f"GRANT {priv}{col} ON {table} TO {role}")
        for table in sorted(EXTERNAL[role]):
            for priv, cols in sorted(EXTERNAL[role][table].items()):
                col = f" ({', '.join(cols)})" if cols else ""
                out.append(f"GRANT {priv}{col} ON {table} TO {role}")
        seqs = SEQUENCE_USAGE.get(role, ())
        if seqs:
            out.append(f"GRANT USAGE ON SEQUENCE {', '.join(seqs)} TO {role}")
    # SECURITY DEFINER functions (rendered since revision 0024): never PUBLIC, exactly FUNCTIONS
    for sig, allowed in sorted(FUNCTIONS.items()):
        # never from the definer: it OWNS the functions, revoking would strip its own EXECUTE
        out.append(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC, {API}, {WORKER}")
        for role in allowed:
            out.append(f"GRANT EXECUTE ON FUNCTION public.{sig} TO {role}")
    return out
