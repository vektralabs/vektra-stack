"""Assess grant matrix (plan assess-data-crypto 3.1, INT-002).

Revokes everything the three assess roles hold on the assess tables, then grants exactly the
matrix of ``vektra_assess/db/grants.py``. The statements below are that module's
``render_statements()`` output committed as text: the migration never imports application
code, and ``tests/integration/test_grant_matrix.py`` fails if the two drift apart.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_outage_incidents, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_snapshots, assess_start_attempts, assess_transcript_download_tokens, assess_transcripts, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE CREATE ON SCHEMA public FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "GRANT INSERT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, updated_at, teacher_config_sealed) ON assess_activity_config TO vektra_assess_api",
    "GRANT SELECT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, teacher_config_set, updated_at) ON assess_activity_config TO vektra_assess_api",
    "GRANT UPDATE (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, recipients_extra, teacher_config_kid, updated_at, teacher_config_sealed) ON assess_activity_config TO vektra_assess_api",
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
    "GRANT INSERT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_state TO vektra_assess_api",
    "GRANT INSERT ON assess_reset_grants TO vektra_assess_api",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_api",
    "GRANT UPDATE ON assess_reset_grants TO vektra_assess_api",
    "GRANT INSERT ON assess_sessions TO vektra_assess_api",
    "GRANT SELECT ON assess_sessions TO vektra_assess_api",
    "GRANT UPDATE (seq, epoch, last_activity_at) ON assess_sessions TO vektra_assess_api",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_api",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_api",
    "GRANT SELECT ON assess_sites TO vektra_assess_api",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_api",
    "GRANT INSERT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_api",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_render_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_api",
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
    "GRANT DELETE ON assess_setup_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT INSERT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT UPDATE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_worker",
    "GRANT SELECT ON assess_sites TO vektra_assess_worker",
    "GRANT UPDATE (release, min_plugin_version) ON assess_sites TO vektra_assess_worker",
    "GRANT DELETE ON assess_snapshots TO vektra_assess_worker",
    "GRANT INSERT ON assess_snapshots TO vektra_assess_worker",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_worker",
    "GRANT DELETE ON assess_start_attempts TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcripts TO vektra_assess_worker",
    "GRANT INSERT ON assess_transcripts TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_worker",
    "GRANT UPDATE ON assess_transcripts TO vektra_assess_worker",
    "GRANT DELETE ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT ON assess_turns TO vektra_assess_worker",
    "GRANT SELECT ON assess_turns TO vektra_assess_worker",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_definer",
    "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq TO vektra_assess_definer",
)


DEFINER_0014 = (
    "GRANT SELECT ON assess_sessions, assess_session_handoff, assess_snapshots, "
    "assess_turns TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns, assess_jobs, assess_audit_events "
    "TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at) "
    "ON assess_sessions TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_jobs_id_seq, assess_audit_events_id_seq "
    "TO vektra_assess_definer",
)


def upgrade() -> None:
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    # back to the 0014 state: nothing granted except what 0014 gave the definer
    op.execute(GRANT_STATEMENTS[0])
    op.execute(GRANT_STATEMENTS[1])
    for stmt in DEFINER_0014:
        op.execute(stmt)
