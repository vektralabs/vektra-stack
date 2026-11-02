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
    "assess_audit_events",
    "assess_consumed_jtis",
    "assess_deliveries",
    "assess_enrolment_checks",
    "assess_erasure_records",
    "assess_extra_time",
    "assess_gate_inputs",
    "assess_heartbeat",
    "assess_jobs",
    "assess_outage_incidents",
    "assess_render_checks",
    "assess_render_state",
    "assess_reset_grants",
    "assess_session_handoff",
    "assess_session_keys",
    "assess_sessions",
    "assess_setup_checks",
    "assess_site_capabilities",
    "assess_site_keys",
    "assess_sites",
    "assess_snapshots",
    "assess_start_attempts",
    "assess_transcript_download_tokens",
    "assess_transcripts",
    "assess_turns",
)

#: BIGSERIAL sequences: INSERT needs USAGE on them.
SEQUENCES: tuple[str, ...] = (
    "assess_audit_events_id_seq",
    "assess_enrolment_checks_id_seq",
    "assess_erasure_records_id_seq",
    "assess_jobs_id_seq",
    "assess_outage_incidents_id_seq",
    "assess_render_checks_id_seq",
    "assess_setup_checks_id_seq",
    "assess_start_attempts_id_seq",
)

#: columns of assess_activity_config the API may read: everything but the sealed config.
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
    "recipients_extra",
    "teacher_config_kid",
    "teacher_config_set",
    "updated_at",
)
#: columns the API may write: it may SEAL teacher config, never read it back (OQ-034).
_CONFIG_WRITE = (
    *(c for c in _CONFIG_READ if c != "teacher_config_set"),
    "teacher_config_sealed",
)
_ALL: None = None

MATRIX: Mapping[str, Mapping[str, Privs]] = {
    API: {
        "assess_jobs": {"INSERT": _ALL},
        "assess_render_checks": {"INSERT": _ALL},
        "assess_render_state": {"SELECT": _ALL},
        "assess_gate_inputs": {"SELECT": _ALL},
        "assess_enrolment_checks": {"SELECT": _ALL},
        "assess_heartbeat": {"SELECT": _ALL},
        "assess_sites": {"SELECT": _ALL},
        "assess_site_keys": {"SELECT": _ALL},
        "assess_site_capabilities": {"SELECT": _ALL},
        "assess_sessions": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": ("seq", "epoch", "last_activity_at"),
        },
        "assess_start_attempts": {"INSERT": _ALL},
        "assess_audit_events": {"INSERT": _ALL},
        "assess_erasure_records": {"INSERT": _ALL},
        "assess_activity_config": {
            "SELECT": _CONFIG_READ,
            "INSERT": _CONFIG_WRITE,
            "UPDATE": _CONFIG_WRITE,
        },
        "assess_extra_time": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL},
        "assess_reset_grants": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL},
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
        "assess_deliveries": {"SELECT": _ALL},
        # snapshots, turns, session_keys, session_handoff: no privilege at all
    },
    WORKER: {
        # plumbing and status
        "assess_jobs": {"SELECT": _ALL, "INSERT": _ALL, "UPDATE": _ALL, "DELETE": _ALL},
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
        "assess_consumed_jtis": {"SELECT": _ALL, "DELETE": _ALL},
        "assess_transcript_download_tokens": {"SELECT": _ALL, "DELETE": _ALL},
        # registry and configuration (the worker reads the sealed teacher config)
        "assess_sites": {"SELECT": _ALL, "UPDATE": ("release", "min_plugin_version")},
        "assess_site_keys": {"SELECT": _ALL},
        "assess_site_capabilities": {
            "SELECT": _ALL,
            "INSERT": _ALL,
            "UPDATE": _ALL,
            "DELETE": _ALL,
        },
        "assess_activity_config": {"SELECT": _ALL},
        "assess_extra_time": {"SELECT": _ALL},
        "assess_reset_grants": {"SELECT": _ALL, "UPDATE": ("consumed_at",)},
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
            "UPDATE": ("seq", "last_activity_at", "state", "close_reason", "closed_at"),
        },
        "assess_session_handoff": {"SELECT": _ALL, "DELETE": _ALL},
        "assess_snapshots": {"SELECT": _ALL},
        "assess_turns": {"SELECT": _ALL, "INSERT": _ALL},
        "assess_jobs": {"INSERT": _ALL},
        "assess_audit_events": {"INSERT": _ALL},
    },
}

#: sequences each role may USE (BIGSERIAL inserts).
SEQUENCE_USAGE: Mapping[str, tuple[str, ...]] = {
    API: (
        "assess_audit_events_id_seq",
        "assess_erasure_records_id_seq",
        "assess_jobs_id_seq",
        "assess_render_checks_id_seq",
        "assess_start_attempts_id_seq",
    ),
    WORKER: SEQUENCES,
    DEFINER: ("assess_audit_events_id_seq", "assess_jobs_id_seq"),
}

#: SECURITY DEFINER functions (signature -> roles with EXECUTE). Never PUBLIC.
FUNCTIONS: Mapping[str, tuple[str, ...]] = {
    "get_session_material(uuid,integer)": (API,),
    "assess_append_turn(uuid,integer,integer,text,text,bytea,text,text)": (API,),
    "assess_close_session(uuid,integer,text)": (API, WORKER),
}


def render_statements() -> list[str]:
    """The SQL of revision 0015, in order: revoke everything, then grant the matrix."""
    out: list[str] = []
    tables = ", ".join(TABLES)
    roles = ", ".join(ROLES)
    out.append(f"REVOKE ALL ON {tables} FROM {roles}")
    out.append(f"REVOKE ALL ON SEQUENCE {', '.join(SEQUENCES)} FROM {roles}")
    out.append(f"REVOKE CREATE ON SCHEMA public FROM {roles}")
    for role in ROLES:
        for table in sorted(MATRIX[role]):
            for priv, cols in sorted(MATRIX[role][table].items()):
                col = f" ({', '.join(cols)})" if cols else ""
                out.append(f"GRANT {priv}{col} ON {table} TO {role}")
        seqs = SEQUENCE_USAGE.get(role, ())
        if seqs:
            out.append(f"GRANT USAGE ON SEQUENCE {', '.join(seqs)} TO {role}")
    return out
