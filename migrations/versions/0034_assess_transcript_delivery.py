"""Transcripts, the delivery outbox and download tokens (plan assess-transcript-delivery).

- ``assess_transcripts`` gains ``turns_digest`` (SHA-256 over the turn hashes, printed in the
  footer), ``filename`` (BR-016) and ``byte_size``; a live (not shredded) row must carry all three.
- ``assess_deliveries`` is re-created in the shape of the plan (nothing ever wrote the plan-2
  draft): one row per (transcript, round, recipient user id), ``kind`` (``transcript`` or plan 11's
  ``reset_notice``), the status machine ``PENDING|RETRYING|SENT|FAILED|ABANDONED|REPLACED`` and
  the attempt columns. Addresses are NOT stored: they are re-resolved from Moodle at send time
  (REQ-082). ``SENT`` iff ``sent_at``; a due-rows index on ``(status, not_before)``.
- ``assess_transcript_download_tokens`` gains ``audience_role`` (``owner`` / ``grader``) and the
  site; the raw jti is never stored (``jti_hash``).
- ``assess_turn_notes``: the post-check mark codes, the contest entries (label + status) and the
  reply language of a bot turn, written by the API turn driver in the turn's own transaction (the turn row is never
  updated, BR-015); the transcript renders the markers and "Punti contestati" from it.
- ``assess_enqueue_resend(tid, actor)``: SECURITY DEFINER, round n+1 for the recipients of the
  last round (never a REPLACED one); the caller cannot name a recipient (REQ-082). Serialised per
  transcript by an advisory lock.

The whole grant matrix is re-rendered.

Revision ID: 0034
Revises: 0033
"""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_RESEND = "public.assess_enqueue_resend(uuid,text)"

GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
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
    "GRANT UPDATE (epoch, tokens_used) ON assess_sessions TO vektra_assess_api",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_api",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_api",
    "GRANT SELECT (id, moodle_site_uuid, base_url, release, min_plugin_version, created_at, disabled_at, last_seen_release, moodle_origin) ON assess_sites TO vektra_assess_api",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_api",
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
    "GRANT DELETE ON assess_start_breaker TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT UPDATE ON assess_start_breaker TO vektra_assess_worker",
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
    "REVOKE ALL ON FUNCTION public.assess_close_session(uuid,integer,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_end_outage(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_end_outage(uuid,integer) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_enqueue_resend(uuid,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enqueue_resend(uuid,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)

RESEND = """
    CREATE FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_round integer;
        v_site uuid;
        v_sid uuid;
        v_n integer;
    BEGIN
        IF p_actor IS NULL OR length(p_actor) > 64 THEN
            RETURN 0;
        END IF;
        SELECT s.site_id, s.id INTO v_site, v_sid
          FROM public.assess_transcripts t JOIN public.assess_sessions s ON s.id = t.session_id
         WHERE t.id = p_tid AND t.ciphertext IS NOT NULL;
        IF NOT FOUND THEN
            RETURN 0;
        END IF;
        -- two resends of one transcript never compute the same round
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('assess_resend:' || p_tid::text, 0));
        SELECT max(d.round) INTO v_round FROM public.assess_deliveries d
         WHERE d.transcript_id = p_tid AND d.kind = 'transcript';
        IF v_round IS NULL THEN
            RETURN 0;  -- never delivered to anyone: nothing to resend
        END IF;
        INSERT INTO public.assess_deliveries
            (transcript_id, kind, round, recipient_kind, recipient_userid, status)
        SELECT p_tid, 'transcript', v_round + 1, d.recipient_kind, d.recipient_userid, 'PENDING'
          FROM public.assess_deliveries d
         WHERE d.transcript_id = p_tid AND d.kind = 'transcript' AND d.round = v_round
           AND d.status <> 'REPLACED';
        GET DIAGNOSTICS v_n = ROW_COUNT;
        IF v_n = 0 THEN
            RETURN 0;
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data)
        VALUES (v_site, v_sid, 'assess_transcript_resend', NULL, 'api',
                pg_catalog.jsonb_build_object('round', v_round + 1, 'recipients', v_n,
                                              'actor', p_actor));
        RETURN v_round + 1;
    END
    $$
"""


def upgrade() -> None:
    op.execute("ALTER TABLE assess_transcripts ADD COLUMN turns_digest TEXT NULL")
    op.execute("ALTER TABLE assess_transcripts ADD COLUMN filename TEXT NULL")
    op.execute("ALTER TABLE assess_transcripts ADD COLUMN byte_size INTEGER NULL")
    op.execute(
        "ALTER TABLE assess_transcripts ADD CONSTRAINT ck_assess_transcripts_live_fields "
        "CHECK (ciphertext IS NULL OR (turns_digest IS NOT NULL AND filename IS NOT NULL "
        "AND byte_size IS NOT NULL)) NOT VALID"  # rows of no generator before this: exempt
    )
    op.execute("DROP TABLE assess_deliveries")
    op.execute("""
        CREATE TABLE assess_deliveries (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            transcript_id      UUID NULL REFERENCES assess_transcripts(id),
            kind               TEXT NOT NULL DEFAULT 'transcript',
            round              INTEGER NOT NULL DEFAULT 1,
            recipient_kind     TEXT NOT NULL,
            recipient_userid   BIGINT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'PENDING',
            attempts           INTEGER NOT NULL DEFAULT 0,
            not_before         TIMESTAMPTZ NOT NULL DEFAULT now(),
            attempt_started_at TIMESTAMPTZ NULL,
            last_error_class   TEXT NULL,
            smtp_code          INTEGER NULL,
            message_id         TEXT NULL,
            sent_at            TIMESTAMPTZ NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_assess_deliveries_round UNIQUE (transcript_id, round, recipient_userid),
            CONSTRAINT ck_assess_deliveries_kind CHECK (kind IN ('transcript', 'reset_notice')),
            CONSTRAINT ck_assess_deliveries_transcript
                CHECK ((kind = 'transcript') = (transcript_id IS NOT NULL)),
            CONSTRAINT ck_assess_deliveries_recipient_kind
                CHECK (recipient_kind IN ('grader', 'student')),
            CONSTRAINT ck_assess_deliveries_status CHECK (status IN
                ('PENDING', 'RETRYING', 'SENT', 'FAILED', 'ABANDONED', 'REPLACED')),
            CONSTRAINT ck_assess_deliveries_sent CHECK ((status = 'SENT') = (sent_at IS NOT NULL)),
            CONSTRAINT ck_assess_deliveries_round CHECK (round >= 1),
            CONSTRAINT ck_assess_deliveries_attempts CHECK (attempts >= 0)
        )
    """)
    op.execute(
        "CREATE INDEX ix_assess_deliveries_due ON assess_deliveries (status, not_before)"
    )
    op.execute(
        "ALTER TABLE assess_transcript_download_tokens "
        "ADD COLUMN audience_role TEXT NOT NULL DEFAULT 'owner' "
        "CONSTRAINT ck_assess_download_tokens_role CHECK (audience_role IN ('owner', 'grader'))"
    )
    op.execute(
        "ALTER TABLE assess_transcript_download_tokens ALTER COLUMN audience_role DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE assess_transcript_download_tokens ADD COLUMN site_id UUID NULL "
        "REFERENCES assess_sites(id)"
    )
    op.execute("""
        CREATE TABLE assess_turn_notes (
            session_id  UUID NOT NULL,
            turn_no     INTEGER NOT NULL,
            marks       TEXT[] NOT NULL DEFAULT '{}',
            contests    JSONB NOT NULL DEFAULT '[]'::jsonb,
            lang        TEXT NULL CHECK (lang IN ('it', 'en', 'other')),
            PRIMARY KEY (session_id, turn_no),
            FOREIGN KEY (session_id, turn_no)
                REFERENCES assess_turns(session_id, turn_no) ON DELETE CASCADE,
            CONSTRAINT ck_assess_turn_notes_small
                CHECK (pg_column_size(contests) < 4096 AND cardinality(marks) <= 16)
        )
    """)
    op.execute(RESEND)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema (revoked again by the matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {SIG_RESEND} OWNER TO {DEFINER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG_RESEND} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIG_RESEND}")
    op.execute("DROP TABLE assess_turn_notes")
    op.execute("ALTER TABLE assess_transcript_download_tokens DROP COLUMN site_id")
    op.execute(
        "ALTER TABLE assess_transcript_download_tokens DROP COLUMN audience_role"
    )
    op.execute("DROP TABLE assess_deliveries")
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
    op.execute(
        "ALTER TABLE assess_transcripts DROP CONSTRAINT ck_assess_transcripts_live_fields"
    )
    for col in ("turns_digest", "filename", "byte_size"):
        op.execute(f"ALTER TABLE assess_transcripts DROP COLUMN {col}")
    for stmt in (
        "REVOKE ALL ON assess_transcripts FROM vektra_assess_definer",
        "REVOKE ALL ON assess_deliveries FROM vektra_assess_definer",
    ):
        op.execute(stmt)
    from importlib import util
    from pathlib import Path

    spec = util.spec_from_file_location(
        "rev_0033", Path(__file__).with_name("0033_assess_site_moodle_origin.py")
    )
    assert spec is not None and spec.loader is not None
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for stmt in mod.GRANT_STATEMENTS:
        op.execute(stmt)
