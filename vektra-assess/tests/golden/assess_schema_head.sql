CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

CREATE FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$

    DECLARE
        v_seq integer;
        v_pending timestamptz;
        v_deadline timestamptz;
        v_grace integer;
        v_last_no integer;
        v_expected_prev text;
        v_last_role text;
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
        IF p_role = 'bot' AND v_pending IS NULL THEN
            RETURN NULL;  -- no student turn is waiting: a second reply is never appended
        END IF;
        SELECT t.turn_no, t.turn_hash, t.role INTO v_last_no, v_expected_prev, v_last_role
        FROM public.assess_turns t WHERE t.session_id = p_sid
        ORDER BY t.turn_no DESC LIMIT 1;
        IF NOT FOUND THEN
            RETURN NULL;  -- no opening turn yet: nothing to chain onto
        END IF;
        IF p_role = 'bot' AND v_last_role <> 'student' THEN
            RETURN NULL;  -- a bot turn answers exactly one student turn
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
    $$;

ALTER FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_audit_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            RAISE EXCEPTION 'assess_audit_events is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$;

ALTER FUNCTION public.assess_audit_events_append_only() OWNER TO <owner>;

CREATE FUNCTION public.assess_audit_sealed_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_action text;
        v_data jsonb;
        v_old text[] := '{}';
        v_new text[] := coalesce(NEW.refs, '{}');
        i integer;
    BEGIN
        -- the worker's key rotation re-seals blob and kid (its only UPDATE columns): not a
        -- teacher's change. Decided by the login role, which a SECURITY DEFINER function keeps
        -- in session_user; every other write, whatever columns it names, is audited.
        IF TG_OP = 'UPDATE' AND session_user = 'vektra_assess_worker' THEN
            RETURN NULL;
        END IF;
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
    $$;

ALTER FUNCTION public.assess_audit_sealed_change() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
        VALUES (v_site, p_sid, 'assess_close', p_reason,
                CASE WHEN session_user = 'vektra_assess_worker'
                     THEN 'worker' ELSE 'api' END);
        RETURN true;
    END
    $$;

ALTER FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_configured_site_count() RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT count(*) FROM public.assess_privacy_config p
        JOIN public.assess_sites s ON s.id = p.site_id
        WHERE s.disabled_at IS NULL AND p.notice_url ~ '^https://[^/?#@\s\[\]]+'
    $$;

ALTER FUNCTION public.assess_configured_site_count() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
        -- C and the spent outage: the one definition (revision 0032)
        v_cap := public.assess_outage_cap(v_duration);
        v_spent := public.assess_outage_spent(p_sid);
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
    $$;

ALTER FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
        -- two resends of one transcript never compute the same round; the worker's recipient
        -- fan-out takes the same lock
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('assess_resend:' || p_tid::text, 0));
        SELECT max(d.round) INTO v_round FROM public.assess_deliveries d
         WHERE d.transcript_id = p_tid AND d.kind = 'transcript';
        IF v_round IS NULL THEN
            RETURN 0;  -- never delivered to anyone: nothing to resend
        END IF;
        IF p_min_interval_s > 0 AND EXISTS (SELECT 1 FROM public.assess_deliveries d
                    WHERE d.transcript_id = p_tid AND d.kind = 'transcript'
                      AND d.created_at > pg_catalog.now()
                          - pg_catalog.make_interval(secs => p_min_interval_s)) THEN
            RETURN -1;
        END IF;
        INSERT INTO public.assess_deliveries
            (transcript_id, kind, round, recipient_kind, recipient_userid, recipient_ref, status)
        SELECT p_tid, 'transcript', v_round + 1, d.recipient_kind, d.recipient_userid,
               d.recipient_ref, 'PENDING'
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
    $$;

ALTER FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
    $$;

ALTER FUNCTION public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_live_session_count() RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT count(*) FROM public.assess_sessions WHERE state <> 'CLOSED'
    $$;

ALTER FUNCTION public.assess_live_session_count() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_metrics_snapshot() RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT jsonb_build_object(
            'sessions', COALESCE((SELECT jsonb_object_agg(state, n) FROM (
                SELECT state, count(*) AS n FROM public.assess_sessions GROUP BY state) s),
                '{}'::jsonb),
            'closed', COALESCE((SELECT jsonb_object_agg(close_reason, n) FROM (
                SELECT close_reason, count(*) AS n FROM public.assess_sessions
                WHERE state = 'CLOSED' GROUP BY close_reason) c), '{}'::jsonb),
            'deliveries', COALESCE((SELECT jsonb_object_agg(status, n) FROM (
                SELECT status, count(*) AS n FROM public.assess_deliveries
                WHERE kind <> 'smtp_test' GROUP BY status) d), '{}'::jsonb),
            'retrying_oldest_s', (SELECT extract(epoch FROM now() - min(created_at))
                FROM public.assess_deliveries WHERE status = 'RETRYING'
                AND kind <> 'smtp_test'),
            'tls_failures', (SELECT count(*) FROM public.assess_deliveries
                WHERE last_error_class = 'tls' AND kind <> 'smtp_test'
                AND status IN ('PENDING', 'RETRYING', 'FAILED')),
            'capacity_refusals', (SELECT count(*) FROM public.assess_start_attempts
                WHERE outcome IN ('rolled_back', 'refused') AND reason_code = 'capacity'),
            'rollbacks', COALESCE((SELECT jsonb_object_agg(reason, n) FROM (
                SELECT COALESCE(reason_code, 'none') AS reason, count(*) AS n
                FROM public.assess_start_attempts WHERE outcome = 'rolled_back'
                GROUP BY 1) r), '{}'::jsonb),
            'jobs_oldest_ready_s', (SELECT extract(epoch FROM now() - min(not_before))
                FROM public.assess_jobs WHERE state = 'ready' AND not_before <= now()),
            'worker_heartbeat_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'worker'),
            'smtp_probe_ok', (SELECT CASE
                    WHEN details->>'state' = 'busy' THEN (details->'last'->>'ok')::boolean
                    ELSE (details->>'ok')::boolean END
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'smtp_probe_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'purge_last_success', (SELECT (details->>'last_success')::double precision
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_rows_deleted', (SELECT (details->>'rows_deleted')::bigint
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_last_ok', (SELECT (details->>'ok')::boolean
                FROM public.assess_heartbeat WHERE component = 'purge')
        )
    $$;

ALTER FUNCTION public.assess_metrics_snapshot() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_outage_cap(p_duration_s integer) RETURNS integer
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT least(900, coalesce(p_duration_s, 0) / 2)
    $$;

ALTER FUNCTION public.assess_outage_cap(p_duration_s integer) OWNER TO <owner>;

CREATE FUNCTION public.assess_outage_spent(p_sid uuid) RETURNS double precision
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT coalesce(sum(extract(epoch FROM coalesce(i.ended_at, now()) - i.started_at)),
                        0)::double precision
        FROM public.assess_outage_incidents i WHERE i.session_id = p_sid
    $$;

ALTER FUNCTION public.assess_outage_spent(p_sid uuid) OWNER TO <owner>;

CREATE FUNCTION public.assess_pending_replies(p_min_age_s double precision, p_limit integer) RETURNS TABLE(id uuid, site_id uuid)
    LANGUAGE sql SECURITY DEFINER
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
    $$;

ALTER FUNCTION public.assess_pending_replies(p_min_age_s double precision, p_limit integer) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_rollback_starting(p_sid uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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
             + (SELECT count(*) FROM public.assess_session_keys WHERE session_id = p_sid)
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
    $$;

ALTER FUNCTION public.assess_rollback_starting(p_sid uuid) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_session_exists(p_sid uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT EXISTS (SELECT 1 FROM public.assess_sessions WHERE id = p_sid)
    $$;

ALTER FUNCTION public.assess_session_exists(p_sid uuid) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_sessions_drop_handoff_on_close() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF NEW.state = 'CLOSED' THEN
                DELETE FROM public.assess_session_handoff WHERE session_id = NEW.id;
            END IF;
            RETURN NEW;
        END
        $$;

ALTER FUNCTION public.assess_sessions_drop_handoff_on_close() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_sessions_no_revival() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF OLD.state = 'CLOSED' AND NEW.state <> 'CLOSED' THEN
                RAISE EXCEPTION 'assess session % is CLOSED and cannot be reopened', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$;

ALTER FUNCTION public.assess_sessions_no_revival() OWNER TO <owner>;

CREATE FUNCTION public.assess_sessions_number() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            -- session #N+1 after a reset (REQ-087): number it under a lock on the triple,
            -- so no caller has to compute max()+1 itself
            IF NEW.session_no IS NULL THEN
                PERFORM pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        NEW.site_id::text || ':' || NEW.student_userid || ':' || NEW.cmid, 0));
                SELECT coalesce(max(s.session_no), 0) + 1 INTO NEW.session_no
                FROM public.assess_sessions s
                WHERE s.site_id = NEW.site_id AND s.student_userid = NEW.student_userid
                  AND s.cmid = NEW.cmid;
            END IF;
            RETURN NEW;
        END
        $$;

ALTER FUNCTION public.assess_sessions_number() OWNER TO <owner>;

CREATE FUNCTION public.assess_sessions_require_opening_turn() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF NEW.state IN ('ACTIVE', 'OUTAGE', 'CLOSED') AND NOT EXISTS (
                SELECT 1 FROM public.assess_turns t
                WHERE t.session_id = NEW.id AND t.kind = 'opening'
            ) THEN
                RAISE EXCEPTION
                    'assess session % is % without an opening turn (REQ-067)',
                    NEW.id, NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END
        $$;

ALTER FUNCTION public.assess_sessions_require_opening_turn() OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_site_of_key(p_key_id uuid, p_purpose text) RETURNS TABLE(id uuid, min_plugin_version text, disabled boolean)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT s.id, s.min_plugin_version, s.disabled_at IS NOT NULL
        FROM public.assess_site_keys k
        JOIN public.assess_sites s ON s.id = k.site_id
        WHERE k.api_key_id = p_key_id AND k.purpose = p_purpose
    $$;

ALTER FUNCTION public.assess_site_of_key(p_key_id uuid, p_purpose text) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.assess_snapshots_immutable() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            RAISE EXCEPTION 'assess_snapshots rows are immutable (BR-015)'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$;

ALTER FUNCTION public.assess_snapshots_immutable() OWNER TO <owner>;

CREATE FUNCTION public.assess_transcript_site(p_tid uuid) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT s.site_id FROM public.assess_transcripts t
        JOIN public.assess_sessions s ON s.id = t.session_id
        WHERE t.id = p_tid
    $$;

ALTER FUNCTION public.assess_transcript_site(p_tid uuid) OWNER TO vektra_assess_definer;

CREATE FUNCTION public.get_session_material(p_sid uuid, p_epoch integer) RETURNS TABLE(sealed_dek bytea, handoff_kid text, snapshot_ciphertext bytea, snapshot_hash text, seq integer, turn_nos integer[], turn_roles text[], turn_kinds text[], turn_ciphertexts bytea[], turn_hashes text[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
            SELECT h.sealed_dek, h.handoff_kid, sn.ciphertext, sn.content_hash, s.seq,
                   array(SELECT t.turn_no FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.role FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.kind FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.ciphertext FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.turn_hash FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no)
            FROM public.assess_sessions s
            JOIN public.assess_session_handoff h ON h.session_id = s.id
            JOIN public.assess_snapshots sn ON sn.session_id = s.id
            WHERE s.id = p_sid
              AND s.state IN ('ACTIVE', 'OUTAGE')
              AND s.epoch = p_epoch
              AND h.epoch = s.epoch
        $$;

ALTER FUNCTION public.get_session_material(p_sid uuid, p_epoch integer) OWNER TO vektra_assess_definer;

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

ALTER TABLE public.alembic_version OWNER TO <owner>;

CREATE TABLE public.api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key_hash character varying(255) NOT NULL,
    key_preview character varying(4) NOT NULL,
    label character varying(255),
    scopes text[] DEFAULT '{admin}'::text[] NOT NULL,
    rate_limit_rpm integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    expires_at timestamp with time zone,
    CONSTRAINT ck_api_keys_assess_scope_exclusive CHECK (((NOT (scopes && ARRAY['assess:mint'::text, 'assess:teacher'::text, 'assess:privacy-export'::text, 'assess:privacy-erase'::text])) OR (NOT (scopes && ARRAY['admin'::text, 'ingest'::text, 'query'::text])))),
    CONSTRAINT ck_api_keys_scopes CHECK ((scopes <@ ARRAY['admin'::text, 'ingest'::text, 'query'::text, 'assess:mint'::text, 'assess:teacher'::text, 'assess:privacy-export'::text, 'assess:privacy-erase'::text]))
);

ALTER TABLE public.api_keys OWNER TO <owner>;

CREATE TABLE public.assess_activity_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    activity_type text NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    rag_enabled boolean DEFAULT false NOT NULL,
    end_at timestamp with time zone,
    attested_opening_at timestamp with time zone,
    attested_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cap_minutes integer,
    version integer DEFAULT 0 NOT NULL,
    updated_by bigint,
    CONSTRAINT ck_assess_activity_config_cap CHECK (((cap_minutes IS NULL) OR ((cap_minutes >= 5) AND (cap_minutes <= 120)))),
    CONSTRAINT ck_assess_activity_config_rag_off CHECK ((rag_enabled = false)),
    CONSTRAINT ck_assess_activity_config_type CHECK ((activity_type = ANY (ARRAY['quiz'::text, 'assign'::text]))),
    CONSTRAINT ck_assess_activity_config_version CHECK ((version >= 0))
);

ALTER TABLE public.assess_activity_config OWNER TO <owner>;

CREATE TABLE public.assess_activity_config_sealed (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    field text NOT NULL,
    sealed_blob bytea,
    kid text,
    is_set boolean GENERATED ALWAYS AS ((sealed_blob IS NOT NULL)) STORED NOT NULL,
    refs text[],
    item_count integer,
    updated_by bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_activity_config_sealed_field CHECK ((field = ANY (ARRAY['solution'::text, 'pasted_rubric'::text, 'transcript_recipients'::text]))),
    CONSTRAINT ck_assess_activity_config_sealed_kid CHECK (((sealed_blob IS NULL) = (kid IS NULL))),
    CONSTRAINT ck_assess_activity_config_sealed_refs CHECK ((((refs IS NULL) AND (item_count IS NULL)) OR ((field = 'transcript_recipients'::text) AND (sealed_blob IS NOT NULL) AND ((item_count >= 1) AND (item_count <= 3)) AND (cardinality(refs) = item_count))))
);

ALTER TABLE public.assess_activity_config_sealed OWNER TO <owner>;

CREATE TABLE public.assess_audit_events (
    id bigint NOT NULL,
    site_id uuid,
    session_id uuid,
    event text NOT NULL,
    reason_code text,
    actor_kind text NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_audit_events_data_small CHECK ((pg_column_size(data) < 2048))
);

ALTER TABLE public.assess_audit_events OWNER TO <owner>;

CREATE SEQUENCE public.assess_audit_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_audit_events_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_audit_events_id_seq OWNED BY public.assess_audit_events.id;

CREATE TABLE public.assess_consumed_jtis (
    jti_hash text NOT NULL,
    aud text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.assess_consumed_jtis OWNER TO <owner>;

CREATE TABLE public.assess_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    transcript_id uuid,
    kind text DEFAULT 'transcript'::text NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    recipient_kind text NOT NULL,
    recipient_userid bigint,
    status text DEFAULT 'PENDING'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    not_before timestamp with time zone DEFAULT now() NOT NULL,
    attempt_started_at timestamp with time zone,
    last_error_class text,
    smtp_code integer,
    message_id text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    recipient_ref text,
    job_id bigint,
    CONSTRAINT ck_assess_deliveries_attempts CHECK ((attempts >= 0)),
    CONSTRAINT ck_assess_deliveries_job CHECK ((((kind = ANY (ARRAY['teacher_notify'::text, 'operator_notify'::text])) = (job_id IS NOT NULL)) AND ((NOT (kind = ANY (ARRAY['teacher_notify'::text, 'operator_notify'::text]))) OR (recipient_kind = 'address'::text)))),
    CONSTRAINT ck_assess_deliveries_kind CHECK ((kind = ANY (ARRAY['transcript'::text, 'reset_notice'::text, 'teacher_notify'::text, 'operator_notify'::text, 'smtp_test'::text]))),
    CONSTRAINT ck_assess_deliveries_recipient CHECK ((((recipient_kind = 'address'::text) = (recipient_ref IS NOT NULL)) AND ((recipient_kind = 'address'::text) = (recipient_userid IS NULL)))),
    CONSTRAINT ck_assess_deliveries_recipient_kind CHECK ((recipient_kind = ANY (ARRAY['grader'::text, 'student'::text, 'address'::text]))),
    CONSTRAINT ck_assess_deliveries_ref CHECK (((recipient_ref IS NULL) OR (recipient_ref ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_assess_deliveries_round CHECK ((round >= 1)),
    CONSTRAINT ck_assess_deliveries_sent CHECK (((status = 'SENT'::text) = (sent_at IS NOT NULL))),
    CONSTRAINT ck_assess_deliveries_smtp_test CHECK (((kind <> 'smtp_test'::text) OR (recipient_kind = 'address'::text))),
    CONSTRAINT ck_assess_deliveries_status CHECK ((status = ANY (ARRAY['PENDING'::text, 'RETRYING'::text, 'SENT'::text, 'FAILED'::text, 'ABANDONED'::text, 'REPLACED'::text]))),
    CONSTRAINT ck_assess_deliveries_transcript CHECK (((kind = 'transcript'::text) = (transcript_id IS NOT NULL)))
);

ALTER TABLE public.assess_deliveries OWNER TO <owner>;

CREATE TABLE public.assess_enrolment_checks (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    result text NOT NULL,
    CONSTRAINT ck_assess_enrolment_checks_result CHECK ((result = ANY (ARRAY['enrolled'::text, 'not_enrolled'::text, 'unreadable'::text])))
);

ALTER TABLE public.assess_enrolment_checks OWNER TO <owner>;

CREATE SEQUENCE public.assess_enrolment_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_enrolment_checks_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_enrolment_checks_id_seq OWNED BY public.assess_enrolment_checks.id;

CREATE TABLE public.assess_erasure_records (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    subject_userid_hash text,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    rows_deleted jsonb DEFAULT '{}'::jsonb NOT NULL,
    scope text NOT NULL,
    cmid bigint,
    subject_userids bigint[],
    outcome text,
    error_code text,
    CONSTRAINT ck_assess_erasure_records_cleared CHECK (((completed_at IS NULL) OR ((subject_userids IS NULL) AND (subject_userid_hash IS NULL)))),
    CONSTRAINT ck_assess_erasure_records_outcome CHECK ((outcome = ANY (ARRAY['erased'::text, 'failed'::text]))),
    CONSTRAINT ck_assess_erasure_records_scope CHECK ((scope = ANY (ARRAY['subject'::text, 'activity'::text]))),
    CONSTRAINT ck_assess_erasure_records_shape CHECK ((((scope = 'subject'::text) AND ((subject_userid_hash IS NOT NULL) OR (completed_at IS NOT NULL))) OR ((scope = 'activity'::text) AND (cmid IS NOT NULL))))
);

ALTER TABLE public.assess_erasure_records OWNER TO <owner>;

CREATE SEQUENCE public.assess_erasure_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_erasure_records_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_erasure_records_id_seq OWNED BY public.assess_erasure_records.id;

CREATE TABLE public.assess_extra_time (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    extra_seconds integer NOT NULL,
    granted_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_extra_time_nonnegative CHECK ((extra_seconds >= 0))
);

ALTER TABLE public.assess_extra_time OWNER TO <owner>;

CREATE TABLE public.assess_gate_inputs (
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.assess_gate_inputs OWNER TO <owner>;

CREATE TABLE public.assess_heartbeat (
    component text NOT NULL,
    beat_at timestamp with time zone DEFAULT now() NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE public.assess_heartbeat OWNER TO <owner>;

CREATE TABLE public.assess_jobs (
    id bigint NOT NULL,
    kind text NOT NULL,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    dedupe_key text,
    state text DEFAULT 'ready'::text NOT NULL,
    not_before timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    leased_by text,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone,
    last_error_code text,
    CONSTRAINT ck_assess_jobs_args_small CHECK ((pg_column_size(args) < 1024)),
    CONSTRAINT ck_assess_jobs_kind CHECK ((kind = ANY (ARRAY['setup_check'::text, 'gate_refresh'::text, 'render_check'::text, 'transcript_generate'::text, 'transcript_deliver'::text, 'enrolment_recheck'::text, 'purge'::text, 'erasure'::text, 'preflight'::text, 'reconcile_reply'::text, 'teacher_notify'::text, 'operator_notify'::text, 'teacher_gate'::text, 'smtp_test_send'::text]))),
    CONSTRAINT ck_assess_jobs_state CHECK ((state = ANY (ARRAY['ready'::text, 'leased'::text, 'done'::text, 'failed'::text, 'dead'::text])))
)
WITH (autovacuum_vacuum_cost_delay='0', fillfactor='70', autovacuum_vacuum_scale_factor='0.01', autovacuum_vacuum_threshold='200', autovacuum_analyze_scale_factor='0.02');

ALTER TABLE public.assess_jobs OWNER TO <owner>;

CREATE SEQUENCE public.assess_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_jobs_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_jobs_id_seq OWNED BY public.assess_jobs.id;

CREATE TABLE public.assess_model_activations (
    id bigint NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_version character varying(8) NOT NULL,
    report_sha256 character(64) NOT NULL,
    hard_pass boolean NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_by_key_id uuid,
    CONSTRAINT ck_assess_model_activations_pv CHECK (((prompt_version)::text ~ '^[0-9a-f]{8}$'::text)),
    CONSTRAINT ck_assess_model_activations_sha CHECK ((report_sha256 ~ '^[0-9a-f]{64}$'::text))
);

ALTER TABLE public.assess_model_activations OWNER TO <owner>;

CREATE SEQUENCE public.assess_model_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_model_activations_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_model_activations_id_seq OWNED BY public.assess_model_activations.id;

CREATE TABLE public.assess_outage_incidents (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    reason_code text,
    credited_s integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_assess_outage_incidents_credited CHECK ((credited_s >= 0))
);

ALTER TABLE public.assess_outage_incidents OWNER TO <owner>;

CREATE SEQUENCE public.assess_outage_incidents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_outage_incidents_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_outage_incidents_id_seq OWNED BY public.assess_outage_incidents.id;

CREATE TABLE public.assess_privacy_config (
    site_id uuid NOT NULL,
    retention_days integer NOT NULL,
    legal_basis_label text NOT NULL,
    legal_basis_text text NOT NULL,
    notice_url text NOT NULL,
    notice_text text NOT NULL,
    notice_version text NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_privacy_basis CHECK (((length(legal_basis_text) >= 1) AND (length(legal_basis_text) <= 4000))),
    CONSTRAINT ck_assess_privacy_label CHECK (((length(legal_basis_label) >= 1) AND (length(legal_basis_label) <= 200))),
    CONSTRAINT ck_assess_privacy_notice CHECK (((length(notice_text) >= 1) AND (length(notice_text) <= 8000))),
    CONSTRAINT ck_assess_privacy_retention CHECK (((retention_days >= 1) AND (retention_days <= 3650))),
    CONSTRAINT ck_assess_privacy_url CHECK ((((length(notice_url) >= 9) AND (length(notice_url) <= 2000)) AND (notice_url ~~ 'https://%'::text))),
    CONSTRAINT ck_assess_privacy_version CHECK (((length(notice_version) >= 1) AND (length(notice_version) <= 40)))
);

ALTER TABLE public.assess_privacy_config OWNER TO <owner>;

CREATE TABLE public.assess_purge_records (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    subject_ref text NOT NULL,
    session_id uuid NOT NULL,
    transcript_id uuid,
    session_no integer NOT NULL,
    cmid bigint NOT NULL,
    reason text NOT NULL,
    purged_at timestamp with time zone DEFAULT now() NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_assess_purge_records_reason CHECK ((reason = ANY (ARRAY['retention'::text, 'erasure'::text])))
);

ALTER TABLE public.assess_purge_records OWNER TO <owner>;

CREATE SEQUENCE public.assess_purge_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_purge_records_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_purge_records_id_seq OWNED BY public.assess_purge_records.id;

CREATE TABLE public.assess_render_checks (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone
);

ALTER TABLE public.assess_render_checks OWNER TO <owner>;

CREATE SEQUENCE public.assess_render_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_render_checks_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_render_checks_id_seq OWNED BY public.assess_render_checks.id;

CREATE TABLE public.assess_render_state (
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    status text NOT NULL,
    reason_code text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.assess_render_state OWNER TO <owner>;

CREATE TABLE public.assess_reset_grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_at timestamp with time zone,
    session_no integer NOT NULL,
    reason_ct bytea NOT NULL,
    reason_kid text NOT NULL,
    include_previous_turns boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_assess_reset_grants_session_no CHECK ((session_no >= 1))
);

ALTER TABLE public.assess_reset_grants OWNER TO <owner>;

CREATE TABLE public.assess_session_handoff (
    session_id uuid NOT NULL,
    session_state text NOT NULL,
    epoch integer NOT NULL,
    sealed_dek bytea NOT NULL,
    handoff_kid text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_session_handoff_live CHECK ((session_state = ANY (ARRAY['ACTIVE'::text, 'OUTAGE'::text])))
);

ALTER TABLE public.assess_session_handoff OWNER TO <owner>;

CREATE TABLE public.assess_session_keys (
    session_id uuid NOT NULL,
    kek_wrapped bytea,
    kek_kid text NOT NULL,
    key_version integer DEFAULT 1 NOT NULL,
    shredded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_session_keys_shredded CHECK (((kek_wrapped IS NULL) = (shredded_at IS NOT NULL)))
);

ALTER TABLE public.assess_session_keys OWNER TO <owner>;

CREATE TABLE public.assess_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    session_no integer NOT NULL,
    attempt_ref text,
    state text NOT NULL,
    close_reason text,
    starting_expires_at timestamp with time zone,
    deadline_at timestamp with time zone,
    credited_outage_s integer DEFAULT 0 NOT NULL,
    seq integer DEFAULT 0 NOT NULL,
    epoch integer DEFAULT 1 NOT NULL,
    notice_version text,
    regrade_flag boolean DEFAULT false NOT NULL,
    committed_at timestamp with time zone,
    closed_at timestamp with time zone,
    last_activity_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    courseid_hint bigint,
    duration_s integer,
    frozen_policy jsonb,
    preflight_started_at timestamp with time zone,
    pending_reply_since timestamp with time zone,
    reply_grace_s integer DEFAULT 60 NOT NULL,
    tokens_used bigint DEFAULT 0 NOT NULL,
    cap_s integer,
    CONSTRAINT ck_assess_sessions_cap_s CHECK (((cap_s IS NULL) OR (cap_s > 0))),
    CONSTRAINT ck_assess_sessions_close_reason CHECK (((close_reason IS NULL) OR (close_reason = ANY (ARRAY['finished'::text, 'timed_out'::text, 'service_failure'::text, 'budget_exhausted'::text, 'terminated_by_teacher'::text, 'access_revoked'::text])))),
    CONSTRAINT ck_assess_sessions_closed_has_reason CHECK (((state = 'CLOSED'::text) = (close_reason IS NOT NULL))),
    CONSTRAINT ck_assess_sessions_credited_outage CHECK ((credited_outage_s >= 0)),
    CONSTRAINT ck_assess_sessions_duration CHECK (((duration_s IS NULL) OR (duration_s > 0))),
    CONSTRAINT ck_assess_sessions_reply_grace CHECK ((reply_grace_s >= 0)),
    CONSTRAINT ck_assess_sessions_state CHECK ((state = ANY (ARRAY['STARTING'::text, 'ACTIVE'::text, 'OUTAGE'::text, 'CLOSED'::text]))),
    CONSTRAINT ck_assess_sessions_tokens_used CHECK ((tokens_used >= 0))
);

ALTER TABLE public.assess_sessions OWNER TO <owner>;

CREATE TABLE public.assess_setup_check_reports (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    requested_by_key_id uuid,
    status text DEFAULT 'queued'::text NOT NULL,
    lms_release text,
    items jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT ck_assess_setup_check_reports_items_small CHECK ((pg_column_size(items) < 16384)),
    CONSTRAINT ck_assess_setup_check_reports_status CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'failed'::text])))
);

ALTER TABLE public.assess_setup_check_reports OWNER TO <owner>;

CREATE TABLE public.assess_setup_checks (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    item text NOT NULL,
    result text NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_setup_checks_result CHECK ((result = ANY (ARRAY['ok'::text, 'missing'::text])))
);

ALTER TABLE public.assess_setup_checks OWNER TO <owner>;

CREATE SEQUENCE public.assess_setup_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_setup_checks_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_setup_checks_id_seq OWNED BY public.assess_setup_checks.id;

CREATE TABLE public.assess_site_capabilities (
    site_id uuid NOT NULL,
    flag text NOT NULL,
    release text,
    state text NOT NULL,
    evidence_hash text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    reason_code text,
    CONSTRAINT ck_assess_site_capabilities_flag CHECK ((flag = ANY (ARRAY['quiz_overrides'::text, 'assign_extensions'::text, 'assign_cutoff'::text, 'quiz_grace'::text, 'attempt_state'::text, 'per_student_timelimit'::text, 'review_html_filterable'::text, 'rubric_display_settings'::text, 'module_visibility_per_student'::text, 'loginas_crosscheck'::text, 'graders_resolvable'::text, 'enrolment_readable'::text, 'release_state'::text, 'grading_state'::text, 'assign_user_overrides'::text, 'review_options_per_student'::text]))),
    CONSTRAINT ck_assess_site_capabilities_state CHECK ((state = ANY (ARRAY['readable'::text, 'partial'::text, 'unreadable'::text])))
);

ALTER TABLE public.assess_site_capabilities OWNER TO <owner>;

CREATE TABLE public.assess_site_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    purpose text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_site_keys_purpose CHECK ((purpose = ANY (ARRAY['mint'::text, 'teacher'::text, 'privacy-export'::text, 'privacy-erase'::text])))
);

ALTER TABLE public.assess_site_keys OWNER TO <owner>;

CREATE TABLE public.assess_sites (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lms_site_uuid text NOT NULL,
    base_url text NOT NULL,
    release text,
    min_plugin_version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    disabled_at timestamp with time zone,
    probe_manifest jsonb,
    last_seen_release text,
    lms_origin text,
    CONSTRAINT ck_assess_sites_lms_origin CHECK (((lms_origin IS NULL) OR (lms_origin ~ '^https?://([a-z0-9.\-]+|\[[0-9a-f:.]+\])(:[0-9]{1,5})?$'::text)))
);

ALTER TABLE public.assess_sites OWNER TO <owner>;

CREATE TABLE public.assess_snapshots (
    session_id uuid NOT NULL,
    ciphertext bytea NOT NULL,
    content_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.assess_snapshots OWNER TO <owner>;

CREATE TABLE public.assess_start_attempts (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    outcome text NOT NULL,
    reason_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id uuid,
    CONSTRAINT ck_assess_start_attempts_outcome CHECK ((outcome = ANY (ARRAY['requested'::text, 'committed'::text, 'rolled_back'::text, 'refused'::text])))
);

ALTER TABLE public.assess_start_attempts OWNER TO <owner>;

CREATE SEQUENCE public.assess_start_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.assess_start_attempts_id_seq OWNER TO <owner>;

ALTER SEQUENCE public.assess_start_attempts_id_seq OWNED BY public.assess_start_attempts.id;

CREATE TABLE public.assess_start_breaker (
    site_id uuid NOT NULL,
    state text NOT NULL,
    opened_at timestamp with time zone,
    half_open_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_start_breaker_state CHECK ((state = ANY (ARRAY['open'::text, 'closed'::text])))
);

ALTER TABLE public.assess_start_breaker OWNER TO <owner>;

CREATE TABLE public.assess_teacher_gate (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_teacher_gate_small CHECK ((pg_column_size(inputs) < 16384))
);

ALTER TABLE public.assess_teacher_gate OWNER TO <owner>;

CREATE TABLE public.assess_terminations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    reason_ct bytea NOT NULL,
    reason_kid text NOT NULL,
    closed_count integer NOT NULL,
    rolled_back_count integer NOT NULL,
    also_disable boolean NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assess_terminations_closed_count_check CHECK ((closed_count >= 0)),
    CONSTRAINT assess_terminations_rolled_back_count_check CHECK ((rolled_back_count >= 0))
);

ALTER TABLE public.assess_terminations OWNER TO <owner>;

CREATE TABLE public.assess_token_jti (
    jti text NOT NULL,
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    courseid_hint bigint,
    mint_key_id uuid NOT NULL,
    minted_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    CONSTRAINT ck_assess_token_jti_len CHECK (((length(jti) >= 8) AND (length(jti) <= 64)))
);

ALTER TABLE public.assess_token_jti OWNER TO <owner>;

CREATE TABLE public.assess_transcript_download_tokens (
    jti_hash text NOT NULL,
    transcript_id uuid NOT NULL,
    subject_userid bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    audience_role text NOT NULL,
    site_id uuid,
    CONSTRAINT ck_assess_download_tokens_role CHECK ((audience_role = ANY (ARRAY['owner'::text, 'grader'::text])))
);

ALTER TABLE public.assess_transcript_download_tokens OWNER TO <owner>;

CREATE TABLE public.assess_transcripts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    ciphertext bytea,
    dek_wrapped bytea,
    transcript_kid text NOT NULL,
    content_hash text NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    shredded_at timestamp with time zone,
    turns_digest text,
    filename text,
    byte_size integer,
    course_shortname text,
    CONSTRAINT ck_assess_transcripts_shredded CHECK (((ciphertext IS NULL) = (shredded_at IS NOT NULL)))
);

ALTER TABLE public.assess_transcripts OWNER TO <owner>;

CREATE TABLE public.assess_turn_flags (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.assess_turn_flags OWNER TO <owner>;

CREATE TABLE public.assess_turn_notes (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    marks text[] DEFAULT '{}'::text[] NOT NULL,
    contests jsonb DEFAULT '[]'::jsonb NOT NULL,
    lang text,
    CONSTRAINT assess_turn_notes_lang_check CHECK ((lang = ANY (ARRAY['it'::text, 'en'::text, 'other'::text]))),
    CONSTRAINT ck_assess_turn_notes_small CHECK (((pg_column_size(contests) < 4096) AND (cardinality(marks) <= 16)))
);

ALTER TABLE public.assess_turn_notes OWNER TO <owner>;

CREATE TABLE public.assess_turns (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    role text NOT NULL,
    kind text NOT NULL,
    ciphertext bytea NOT NULL,
    turn_hash text NOT NULL,
    prev_hash text NOT NULL,
    marker_flags integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_turns_kind CHECK ((kind = ANY (ARRAY['opening'::text, 'normal'::text, 'interrupted'::text, 'system'::text]))),
    CONSTRAINT ck_assess_turns_no CHECK ((turn_no >= 1)),
    CONSTRAINT ck_assess_turns_role CHECK ((role = ANY (ARRAY['student'::text, 'bot'::text, 'system'::text])))
);

ALTER TABLE public.assess_turns OWNER TO <owner>;

CREATE TABLE public.audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key_id uuid NOT NULL,
    endpoint character varying(255) NOT NULL,
    method character varying(8) NOT NULL,
    status_code integer NOT NULL,
    request_id uuid NOT NULL,
    action character varying(64),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.audit_log OWNER TO <owner>;

CREATE TABLE public.conversation_turns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    turn_number integer NOT NULL,
    question bytea NOT NULL,
    answer bytea,
    response_id uuid,
    model character varying(255),
    prompt_tokens integer,
    completion_tokens integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_conversation_turns_number CHECK ((turn_number >= 1))
);

ALTER TABLE public.conversation_turns OWNER TO <owner>;

CREATE TABLE public.conversations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    namespace_id character varying(64) NOT NULL,
    key_id uuid NOT NULL,
    title character varying(512),
    turn_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    owner_subject character varying(255),
    CONSTRAINT ck_conversations_turn_count CHECK ((turn_count >= 0))
);

ALTER TABLE ONLY public.conversations FORCE ROW LEVEL SECURITY;

ALTER TABLE public.conversations OWNER TO <owner>;

CREATE TABLE public.dashboard_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    student_id character varying(255) NOT NULL,
    course_id character varying(255) NOT NULL,
    token_hash character varying(128) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.dashboard_tokens OWNER TO <owner>;

CREATE TABLE public.document_chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    namespace_id character varying(64) NOT NULL,
    content text NOT NULL,
    embedding public.vector(384) NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    element_type character varying(32) DEFAULT 'text'::character varying NOT NULL,
    content_format character varying(16) DEFAULT 'text'::character varying NOT NULL,
    "position" integer NOT NULL,
    index_version integer DEFAULT 1 NOT NULL,
    parent_id uuid,
    coordinates jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sparse_vector jsonb,
    CONSTRAINT ck_document_chunks_content_format CHECK (((content_format)::text = ANY ((ARRAY['text'::character varying, 'html'::character varying, 'markdown'::character varying])::text[]))),
    CONSTRAINT ck_document_chunks_element_type CHECK (((element_type)::text = ANY ((ARRAY['text'::character varying, 'table'::character varying, 'title'::character varying, 'list'::character varying, 'image'::character varying, 'header'::character varying, 'footer'::character varying, 'caption'::character varying, 'page_break'::character varying, 'formula'::character varying])::text[]))),
    CONSTRAINT ck_document_chunks_position CHECK (("position" >= 0))
);

ALTER TABLE ONLY public.document_chunks FORCE ROW LEVEL SECURITY;

ALTER TABLE public.document_chunks OWNER TO <owner>;

CREATE TABLE public.enrollments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    student_id character varying(255) NOT NULL,
    course_id character varying(255) NOT NULL,
    namespace character varying(64) NOT NULL,
    enrolled_at timestamp with time zone DEFAULT now() NOT NULL,
    enrollment_metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE public.enrollments OWNER TO <owner>;

CREATE TABLE public.feedback (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    response_id uuid NOT NULL,
    citation_id uuid,
    namespace_id character varying(64) NOT NULL,
    key_id uuid NOT NULL,
    rating smallint NOT NULL,
    comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_feedback_rating CHECK (((rating >= 1) AND (rating <= 5)))
);

ALTER TABLE ONLY public.feedback FORCE ROW LEVEL SECURITY;

ALTER TABLE public.feedback OWNER TO <owner>;

CREATE TABLE public.ingest_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid,
    namespace_id character varying(64) NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    phase character varying(16),
    filename character varying(1024) NOT NULL,
    file_size_bytes bigint NOT NULL,
    error_code character varying(32),
    error_message text,
    idempotency_key character varying(255),
    chunk_count integer,
    percentage integer,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_ingest_jobs_percentage CHECK (((percentage IS NULL) OR ((percentage >= 0) AND (percentage <= 100)))),
    CONSTRAINT ck_ingest_jobs_phase CHECK (((phase IS NULL) OR ((phase)::text = ANY ((ARRAY['extracting'::character varying, 'chunking'::character varying, 'embedding'::character varying])::text[])))),
    CONSTRAINT ck_ingest_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'indexed'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE ONLY public.ingest_jobs FORCE ROW LEVEL SECURITY;

ALTER TABLE public.ingest_jobs OWNER TO <owner>;

CREATE TABLE public.namespaces (
    id character varying(64) NOT NULL,
    display_name character varying(255),
    owner_key_id uuid,
    quota_chunks integer,
    quota_documents integer,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    retention_days integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    quota_bytes bigint,
    CONSTRAINT ck_namespaces_quota_bytes CHECK (((quota_bytes IS NULL) OR (quota_bytes > 0))),
    CONSTRAINT ck_namespaces_quota_chunks CHECK (((quota_chunks IS NULL) OR (quota_chunks > 0))),
    CONSTRAINT ck_namespaces_quota_documents CHECK (((quota_documents IS NULL) OR (quota_documents > 0))),
    CONSTRAINT ck_namespaces_retention CHECK (((retention_days IS NULL) OR (retention_days > 0)))
);

ALTER TABLE public.namespaces OWNER TO <owner>;

CREATE TABLE public.query_traces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    response_id uuid NOT NULL,
    namespace_id character varying(64) NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    total_duration_ms integer NOT NULL,
    chunks_retrieved jsonb DEFAULT '[]'::jsonb NOT NULL,
    llm_model character varying(255) NOT NULL,
    prompt_version character varying(8) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    pipeline character varying(32),
    CONSTRAINT ck_query_traces_duration CHECK ((total_duration_ms >= 0))
);

ALTER TABLE ONLY public.query_traces FORCE ROW LEVEL SECURITY;

ALTER TABLE public.query_traces OWNER TO <owner>;

CREATE TABLE public.reindex_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    namespace_id character varying(64) NOT NULL,
    source_index_version integer NOT NULL,
    target_index_version integer NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    total_documents integer DEFAULT 0 NOT NULL,
    processed_documents integer DEFAULT 0 NOT NULL,
    current_document_id uuid,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    chunks_reindexed integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_reindex_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'completed'::character varying, 'failed'::character varying])::text[])))
);

ALTER TABLE public.reindex_jobs OWNER TO <owner>;

CREATE TABLE public.source_documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    namespace_id character varying(64) NOT NULL,
    filename character varying(1024) NOT NULL,
    content_hash character varying(64) NOT NULL,
    content_type character varying(255) NOT NULL,
    file_size_bytes bigint NOT NULL,
    chunk_count integer,
    version integer DEFAULT 1 NOT NULL,
    supersedes_id uuid,
    filename_aliases jsonb DEFAULT '[]'::jsonb NOT NULL,
    deleted_at timestamp with time zone,
    deletion_reason character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_source_documents_deletion_reason CHECK (((deletion_reason IS NULL) OR ((deletion_reason)::text = ANY ((ARRAY['user_request'::character varying, 'superseded'::character varying, 'expired'::character varying, 'pipeline_failure'::character varying])::text[]))))
);

ALTER TABLE ONLY public.source_documents FORCE ROW LEVEL SECURITY;

ALTER TABLE public.source_documents OWNER TO <owner>;

CREATE TABLE public.system_state (
    key character varying(64) NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE public.system_state OWNER TO <owner>;

ALTER TABLE ONLY public.assess_audit_events ALTER COLUMN id SET DEFAULT nextval('public.assess_audit_events_id_seq'::regclass);

ALTER TABLE ONLY public.assess_enrolment_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_enrolment_checks_id_seq'::regclass);

ALTER TABLE ONLY public.assess_erasure_records ALTER COLUMN id SET DEFAULT nextval('public.assess_erasure_records_id_seq'::regclass);

ALTER TABLE ONLY public.assess_jobs ALTER COLUMN id SET DEFAULT nextval('public.assess_jobs_id_seq'::regclass);

ALTER TABLE ONLY public.assess_model_activations ALTER COLUMN id SET DEFAULT nextval('public.assess_model_activations_id_seq'::regclass);

ALTER TABLE ONLY public.assess_outage_incidents ALTER COLUMN id SET DEFAULT nextval('public.assess_outage_incidents_id_seq'::regclass);

ALTER TABLE ONLY public.assess_purge_records ALTER COLUMN id SET DEFAULT nextval('public.assess_purge_records_id_seq'::regclass);

ALTER TABLE ONLY public.assess_render_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_render_checks_id_seq'::regclass);

ALTER TABLE ONLY public.assess_setup_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_setup_checks_id_seq'::regclass);

ALTER TABLE ONLY public.assess_start_attempts ALTER COLUMN id SET DEFAULT nextval('public.assess_start_attempts_id_seq'::regclass);

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_activity_config_sealed
    ADD CONSTRAINT assess_activity_config_sealed_pkey PRIMARY KEY (site_id, cmid, field);

ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_site_id_cmid_key UNIQUE (site_id, cmid);

ALTER TABLE ONLY public.assess_audit_events
    ADD CONSTRAINT assess_audit_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_consumed_jtis
    ADD CONSTRAINT assess_consumed_jtis_pkey PRIMARY KEY (jti_hash);

ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT assess_deliveries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_enrolment_checks
    ADD CONSTRAINT assess_enrolment_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_erasure_records
    ADD CONSTRAINT assess_erasure_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_extra_time
    ADD CONSTRAINT assess_extra_time_pkey PRIMARY KEY (site_id, cmid, student_userid);

ALTER TABLE ONLY public.assess_gate_inputs
    ADD CONSTRAINT assess_gate_inputs_pkey PRIMARY KEY (site_id, student_userid, cmid);

ALTER TABLE ONLY public.assess_heartbeat
    ADD CONSTRAINT assess_heartbeat_pkey PRIMARY KEY (component);

ALTER TABLE ONLY public.assess_jobs
    ADD CONSTRAINT assess_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_model_activations
    ADD CONSTRAINT assess_model_activations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_outage_incidents
    ADD CONSTRAINT assess_outage_incidents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_privacy_config
    ADD CONSTRAINT assess_privacy_config_pkey PRIMARY KEY (site_id);

ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT assess_purge_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_render_checks
    ADD CONSTRAINT assess_render_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_render_state
    ADD CONSTRAINT assess_render_state_pkey PRIMARY KEY (site_id, student_userid, cmid);

ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT assess_reset_grants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_session_handoff
    ADD CONSTRAINT assess_session_handoff_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.assess_session_keys
    ADD CONSTRAINT assess_session_keys_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT assess_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_setup_checks
    ADD CONSTRAINT assess_setup_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_site_capabilities
    ADD CONSTRAINT assess_site_capabilities_pkey PRIMARY KEY (site_id, flag);

ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_api_key_id_key UNIQUE (api_key_id);

ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_sites
    ADD CONSTRAINT assess_sites_lms_site_uuid_key UNIQUE (lms_site_uuid);

ALTER TABLE ONLY public.assess_sites
    ADD CONSTRAINT assess_sites_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_snapshots
    ADD CONSTRAINT assess_snapshots_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.assess_start_attempts
    ADD CONSTRAINT assess_start_attempts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_start_breaker
    ADD CONSTRAINT assess_start_breaker_pkey PRIMARY KEY (site_id);

ALTER TABLE ONLY public.assess_teacher_gate
    ADD CONSTRAINT assess_teacher_gate_pkey PRIMARY KEY (site_id, cmid);

ALTER TABLE ONLY public.assess_terminations
    ADD CONSTRAINT assess_terminations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_token_jti
    ADD CONSTRAINT assess_token_jti_pkey PRIMARY KEY (jti);

ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_pkey PRIMARY KEY (jti_hash);

ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_session_id_key UNIQUE (session_id);

ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_pkey PRIMARY KEY (session_id, turn_no);

ALTER TABLE ONLY public.assess_turn_notes
    ADD CONSTRAINT assess_turn_notes_pkey PRIMARY KEY (session_id, turn_no);

ALTER TABLE ONLY public.assess_turns
    ADD CONSTRAINT assess_turns_pkey PRIMARY KEY (session_id, turn_no);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE public.assess_transcripts
    ADD CONSTRAINT ck_assess_transcripts_live_fields CHECK (((ciphertext IS NULL) OR ((turns_digest IS NOT NULL) AND (filename IS NOT NULL) AND (byte_size IS NOT NULL)))) NOT VALID;

ALTER TABLE ONLY public.conversation_turns
    ADD CONSTRAINT conversation_turns_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dashboard_tokens
    ADD CONSTRAINT dashboard_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT document_chunks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.ingest_jobs
    ADD CONSTRAINT ingest_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.namespaces
    ADD CONSTRAINT namespaces_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.query_traces
    ADD CONSTRAINT query_traces_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.query_traces
    ADD CONSTRAINT query_traces_response_id_key UNIQUE (response_id);

ALTER TABLE ONLY public.reindex_jobs
    ADD CONSTRAINT reindex_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_documents
    ADD CONSTRAINT source_documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.system_state
    ADD CONSTRAINT system_state_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT uq_assess_deliveries_round UNIQUE (transcript_id, round, recipient_userid);

ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT uq_assess_deliveries_round_ref UNIQUE (transcript_id, round, recipient_ref);

ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT uq_assess_purge_records_session UNIQUE (session_id);

ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT uq_assess_reset_grants_session UNIQUE (site_id, cmid, student_userid, session_no);

ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT uq_assess_sessions_id_state UNIQUE (id, state);

ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT uq_assess_sessions_no UNIQUE (site_id, student_userid, cmid, session_no);

ALTER TABLE ONLY public.conversation_turns
    ADD CONSTRAINT uq_conversation_turns_order UNIQUE (conversation_id, turn_number);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT uq_enrollment_student_course UNIQUE (student_id, course_id);

CREATE INDEX idx_dashboard_tokens_hash ON public.dashboard_tokens USING btree (token_hash);

CREATE INDEX ix_api_keys_expires ON public.api_keys USING btree (expires_at) WHERE ((expires_at IS NOT NULL) AND (revoked_at IS NULL));

CREATE INDEX ix_assess_deliveries_due ON public.assess_deliveries USING btree (status, not_before);

CREATE INDEX ix_assess_jobs_kind_claim ON public.assess_jobs USING btree (kind, not_before) WHERE (state = ANY (ARRAY['ready'::text, 'leased'::text]));

CREATE INDEX ix_assess_jobs_root ON public.assess_jobs USING btree (((args ->> 'root_job_id'::text))) WHERE ((args ->> 'root_job_id'::text) IS NOT NULL);

CREATE INDEX ix_assess_model_activations_triple ON public.assess_model_activations USING btree (model, prompt_version) WHERE hard_pass;

CREATE INDEX ix_assess_purge_records_subject ON public.assess_purge_records USING btree (site_id, cmid, subject_ref);

CREATE INDEX ix_assess_purge_records_transcript ON public.assess_purge_records USING btree (transcript_id) WHERE (transcript_id IS NOT NULL);

CREATE INDEX ix_assess_setup_check_reports_site ON public.assess_setup_check_reports USING btree (site_id, created_at DESC);

CREATE INDEX ix_assess_start_attempts_student ON public.assess_start_attempts USING btree (site_id, student_userid, cmid, created_at);

CREATE INDEX ix_assess_terminations_activity ON public.assess_terminations USING btree (site_id, cmid);

CREATE INDEX ix_assess_token_jti_expires ON public.assess_token_jti USING btree (expires_at);

CREATE INDEX ix_assess_transcripts_generated_live ON public.assess_transcripts USING btree (generated_at) WHERE (ciphertext IS NOT NULL);

CREATE INDEX ix_audit_log_action ON public.audit_log USING btree (action) WHERE (action IS NOT NULL);

CREATE INDEX ix_audit_log_created_at ON public.audit_log USING btree (created_at);

CREATE INDEX ix_audit_log_key_id ON public.audit_log USING btree (key_id);

CREATE INDEX ix_conversation_turns_conversation ON public.conversation_turns USING btree (conversation_id);

CREATE INDEX ix_conversation_turns_response ON public.conversation_turns USING btree (response_id) WHERE (response_id IS NOT NULL);

CREATE INDEX ix_conversations_active ON public.conversations USING btree (namespace_id, updated_at) WHERE (deleted_at IS NULL);

CREATE INDEX ix_conversations_key_id ON public.conversations USING btree (key_id);

CREATE INDEX ix_conversations_namespace ON public.conversations USING btree (namespace_id);

CREATE INDEX ix_conversations_owner ON public.conversations USING btree (namespace_id, owner_subject, updated_at DESC) WHERE (deleted_at IS NULL);

CREATE INDEX ix_document_chunks_document_id ON public.document_chunks USING btree (document_id);

CREATE INDEX ix_document_chunks_embedding_hnsw ON public.document_chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');

CREATE INDEX ix_document_chunks_index_version ON public.document_chunks USING btree (index_version);

CREATE INDEX ix_document_chunks_metadata_gin ON public.document_chunks USING gin (metadata);

CREATE INDEX ix_document_chunks_ns_version ON public.document_chunks USING btree (namespace_id, index_version);

CREATE INDEX ix_feedback_created ON public.feedback USING btree (created_at);

CREATE INDEX ix_feedback_namespace ON public.feedback USING btree (namespace_id);

CREATE INDEX ix_feedback_response ON public.feedback USING btree (response_id);

CREATE INDEX ix_ingest_jobs_status ON public.ingest_jobs USING btree (status);

CREATE INDEX ix_query_traces_created ON public.query_traces USING btree (created_at);

CREATE INDEX ix_query_traces_namespace ON public.query_traces USING btree (namespace_id);

CREATE INDEX ix_query_traces_steps_gin ON public.query_traces USING gin (steps);

CREATE INDEX ix_reindex_jobs_namespace_status ON public.reindex_jobs USING btree (namespace_id, status);

CREATE INDEX ix_source_documents_content_hash ON public.source_documents USING btree (content_hash);

CREATE INDEX ix_source_documents_namespace ON public.source_documents USING btree (namespace_id);

CREATE INDEX ix_source_documents_ns_filename ON public.source_documents USING btree (namespace_id, filename) WHERE (deleted_at IS NULL);

CREATE UNIQUE INDEX uq_assess_deliveries_job_ref ON public.assess_deliveries USING btree (job_id, recipient_ref) WHERE (job_id IS NOT NULL);

CREATE UNIQUE INDEX uq_assess_jobs_dedupe ON public.assess_jobs USING btree (dedupe_key) WHERE (state = ANY (ARRAY['ready'::text, 'leased'::text]));

CREATE UNIQUE INDEX uq_assess_render_checks_pending ON public.assess_render_checks USING btree (site_id, student_userid, cmid) WHERE (done_at IS NULL);

CREATE UNIQUE INDEX uq_assess_sessions_one_open ON public.assess_sessions USING btree (site_id, student_userid, cmid) WHERE (state <> 'CLOSED'::text);

CREATE UNIQUE INDEX uq_ingest_jobs_idempotency ON public.ingest_jobs USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);

CREATE UNIQUE INDEX uq_source_documents_filename ON public.source_documents USING btree (namespace_id, filename) WHERE (deleted_at IS NULL);

CREATE UNIQUE INDEX uq_source_documents_hash ON public.source_documents USING btree (namespace_id, content_hash) WHERE (deleted_at IS NULL);

CREATE TRIGGER trg_assess_audit_events_append_only BEFORE DELETE OR UPDATE ON public.assess_audit_events FOR EACH ROW EXECUTE FUNCTION public.assess_audit_events_append_only();

CREATE TRIGGER trg_assess_audit_sealed_change AFTER INSERT OR UPDATE ON public.assess_activity_config_sealed FOR EACH ROW EXECUTE FUNCTION public.assess_audit_sealed_change();

CREATE TRIGGER trg_assess_sessions_drop_handoff BEFORE UPDATE OF state ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_drop_handoff_on_close();

CREATE TRIGGER trg_assess_sessions_no_revival BEFORE UPDATE OF state ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_no_revival();

CREATE TRIGGER trg_assess_sessions_number BEFORE INSERT ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_number();

CREATE CONSTRAINT TRIGGER trg_assess_sessions_opening_turn AFTER INSERT OR UPDATE OF state ON public.assess_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_require_opening_turn();

CREATE TRIGGER trg_assess_snapshots_immutable BEFORE UPDATE ON public.assess_snapshots FOR EACH ROW EXECUTE FUNCTION public.assess_snapshots_immutable();

ALTER TABLE ONLY public.assess_activity_config_sealed
    ADD CONSTRAINT assess_activity_config_sealed_site_id_cmid_fkey FOREIGN KEY (site_id, cmid) REFERENCES public.assess_activity_config(site_id, cmid) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT assess_deliveries_transcript_id_fkey FOREIGN KEY (transcript_id) REFERENCES public.assess_transcripts(id);

ALTER TABLE ONLY public.assess_enrolment_checks
    ADD CONSTRAINT assess_enrolment_checks_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_erasure_records
    ADD CONSTRAINT assess_erasure_records_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_extra_time
    ADD CONSTRAINT assess_extra_time_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_gate_inputs
    ADD CONSTRAINT assess_gate_inputs_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_outage_incidents
    ADD CONSTRAINT assess_outage_incidents_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_privacy_config
    ADD CONSTRAINT assess_privacy_config_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT assess_purge_records_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_render_checks
    ADD CONSTRAINT assess_render_checks_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_render_state
    ADD CONSTRAINT assess_render_state_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT assess_reset_grants_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_session_keys
    ADD CONSTRAINT assess_session_keys_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT assess_sessions_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_requested_by_key_id_fkey FOREIGN KEY (requested_by_key_id) REFERENCES public.api_keys(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_setup_checks
    ADD CONSTRAINT assess_setup_checks_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_site_capabilities
    ADD CONSTRAINT assess_site_capabilities_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.api_keys(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_snapshots
    ADD CONSTRAINT assess_snapshots_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_start_attempts
    ADD CONSTRAINT assess_start_attempts_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_start_breaker
    ADD CONSTRAINT assess_start_breaker_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_teacher_gate
    ADD CONSTRAINT assess_teacher_gate_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_terminations
    ADD CONSTRAINT assess_terminations_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_token_jti
    ADD CONSTRAINT assess_token_jti_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id);

ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_transcript_id_fkey FOREIGN KEY (transcript_id) REFERENCES public.assess_transcripts(id);

ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id);

ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_session_id_turn_no_fkey FOREIGN KEY (session_id, turn_no) REFERENCES public.assess_turns(session_id, turn_no) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_turn_notes
    ADD CONSTRAINT assess_turn_notes_session_id_turn_no_fkey FOREIGN KEY (session_id, turn_no) REFERENCES public.assess_turns(session_id, turn_no) ON DELETE CASCADE;

ALTER TABLE ONLY public.assess_turns
    ADD CONSTRAINT assess_turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.conversation_turns
    ADD CONSTRAINT conversation_turns_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT document_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_documents(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT document_chunks_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.enrollments
    ADD CONSTRAINT enrollments_namespace_fkey FOREIGN KEY (namespace) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.feedback
    ADD CONSTRAINT feedback_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.assess_session_handoff
    ADD CONSTRAINT fk_assess_session_handoff_session FOREIGN KEY (session_id, session_state) REFERENCES public.assess_sessions(id, state) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE ONLY public.namespaces
    ADD CONSTRAINT fk_namespaces_owner_key FOREIGN KEY (owner_key_id) REFERENCES public.api_keys(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.ingest_jobs
    ADD CONSTRAINT ingest_jobs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_documents(id);

ALTER TABLE ONLY public.ingest_jobs
    ADD CONSTRAINT ingest_jobs_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.query_traces
    ADD CONSTRAINT query_traces_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.reindex_jobs
    ADD CONSTRAINT reindex_jobs_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.source_documents
    ADD CONSTRAINT source_documents_namespace_id_fkey FOREIGN KEY (namespace_id) REFERENCES public.namespaces(id);

ALTER TABLE ONLY public.source_documents
    ADD CONSTRAINT source_documents_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES public.source_documents(id);

ALTER TABLE public.assess_activity_config ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_activity_config_sealed ENABLE ROW LEVEL SECURITY;

CREATE POLICY assess_api_site_bound ON public.assess_activity_config TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_activity_config_sealed TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_erasure_records TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_extra_time TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_gate_inputs TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_privacy_config TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_purge_records TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_render_checks TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_render_state TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_reset_grants TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_sessions TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_setup_check_reports TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_site_capabilities TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_site_keys TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_start_attempts TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_start_breaker TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_teacher_gate TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_terminations TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_token_jti TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

CREATE POLICY assess_api_site_bound ON public.assess_transcript_download_tokens TO vektra_assess_api USING ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid)) WITH CHECK ((site_id = (NULLIF(current_setting('app.assess_site_id'::text, true), ''::text))::uuid));

ALTER TABLE public.assess_erasure_records ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_extra_time ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_gate_inputs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_privacy_config ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_purge_records ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_render_checks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_render_state ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_reset_grants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_setup_check_reports ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_site_capabilities ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_site_keys ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_start_attempts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_start_breaker ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_teacher_gate ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_terminations ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_token_jti ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assess_transcript_download_tokens ENABLE ROW LEVEL SECURITY;

CREATE POLICY assess_unbound_roles ON public.assess_activity_config TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_activity_config_sealed TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_erasure_records TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_extra_time TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_gate_inputs TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_privacy_config TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_purge_records TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_render_checks TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_render_state TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_reset_grants TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_sessions TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_setup_check_reports TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_site_capabilities TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_site_keys TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_start_attempts TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_start_breaker TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_teacher_gate TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_terminations TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_token_jti TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

CREATE POLICY assess_unbound_roles ON public.assess_transcript_download_tokens TO vektra_assess_worker, vektra_assess_definer USING (true) WITH CHECK (true);

ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;

CREATE POLICY conversations_namespace_insert ON public.conversations FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY conversations_namespace_isolation ON public.conversations USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

ALTER TABLE public.document_chunks ENABLE ROW LEVEL SECURITY;

CREATE POLICY document_chunks_namespace_insert ON public.document_chunks FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY document_chunks_namespace_isolation ON public.document_chunks USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;

CREATE POLICY feedback_namespace_insert ON public.feedback FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY feedback_namespace_isolation ON public.feedback USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

ALTER TABLE public.ingest_jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY ingest_jobs_namespace_insert ON public.ingest_jobs FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY ingest_jobs_namespace_isolation ON public.ingest_jobs USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

ALTER TABLE public.query_traces ENABLE ROW LEVEL SECURITY;

CREATE POLICY query_traces_assess_worker_only ON public.query_traces AS RESTRICTIVE FOR INSERT TO vektra_assess_worker WITH CHECK ((((namespace_id)::text = 'assess-discussion'::text) AND ((pipeline)::text = 'assess-discussion'::text)));

CREATE POLICY query_traces_namespace_insert ON public.query_traces FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY query_traces_namespace_isolation ON public.query_traces USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

ALTER TABLE public.source_documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY source_documents_namespace_insert ON public.source_documents FOR INSERT WITH CHECK (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

CREATE POLICY source_documents_namespace_isolation ON public.source_documents USING (((COALESCE(current_setting('app.current_namespace'::text, true), ''::text) = ''::text) OR ((namespace_id)::text = current_setting('app.current_namespace'::text, true))));

REVOKE ALL ON FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) TO vektra_assess_api;
GRANT ALL ON FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) TO vektra_assess_worker;

REVOKE ALL ON FUNCTION public.assess_audit_sealed_change() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) TO vektra_assess_api;
GRANT ALL ON FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) TO vektra_assess_worker;

REVOKE ALL ON FUNCTION public.assess_configured_site_count() FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_configured_site_count() TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer) TO vektra_assess_worker;

REVOKE ALL ON FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_live_session_count() FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_live_session_count() TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_metrics_snapshot() FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_metrics_snapshot() TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_pending_replies(p_min_age_s double precision, p_limit integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_pending_replies(p_min_age_s double precision, p_limit integer) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_rollback_starting(p_sid uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_rollback_starting(p_sid uuid) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_session_exists(p_sid uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_session_exists(p_sid uuid) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_sessions_drop_handoff_on_close() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.assess_sessions_require_opening_turn() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.assess_site_of_key(p_key_id uuid, p_purpose text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_site_of_key(p_key_id uuid, p_purpose text) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.assess_transcript_site(p_tid uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.assess_transcript_site(p_tid uuid) TO vektra_assess_api;

REVOKE ALL ON FUNCTION public.get_session_material(p_sid uuid, p_epoch integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_session_material(p_sid uuid, p_epoch integer) TO vektra_assess_api;

GRANT SELECT ON TABLE public.assess_activity_config TO vektra_assess_worker;

GRANT SELECT(id),INSERT(id) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(site_id),INSERT(site_id) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(cmid),INSERT(cmid) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(activity_type),INSERT(activity_type) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(enabled),INSERT(enabled),UPDATE(enabled) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(flags),INSERT(flags),UPDATE(flags) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(rag_enabled),INSERT(rag_enabled),UPDATE(rag_enabled) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(end_at),INSERT(end_at),UPDATE(end_at) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(attested_opening_at),INSERT(attested_opening_at),UPDATE(attested_opening_at) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(attested_by),INSERT(attested_by),UPDATE(attested_by) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(updated_at),INSERT(updated_at),UPDATE(updated_at) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(cap_minutes),INSERT(cap_minutes),UPDATE(cap_minutes) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(version),INSERT(version),UPDATE(version) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT(updated_by),INSERT(updated_by),UPDATE(updated_by) ON TABLE public.assess_activity_config TO vektra_assess_api;

GRANT SELECT ON TABLE public.assess_activity_config_sealed TO vektra_assess_worker;

GRANT SELECT(site_id),INSERT(site_id) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT SELECT(cmid),INSERT(cmid) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT SELECT(field),INSERT(field) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT INSERT(sealed_blob),UPDATE(sealed_blob) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;
GRANT UPDATE(sealed_blob) ON TABLE public.assess_activity_config_sealed TO vektra_assess_worker;

GRANT INSERT(kid),UPDATE(kid) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;
GRANT UPDATE(kid) ON TABLE public.assess_activity_config_sealed TO vektra_assess_worker;

GRANT SELECT(is_set) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT INSERT(refs),UPDATE(refs) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT SELECT(item_count),INSERT(item_count),UPDATE(item_count) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT SELECT(updated_by),INSERT(updated_by),UPDATE(updated_by) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT SELECT(updated_at),INSERT(updated_at),UPDATE(updated_at) ON TABLE public.assess_activity_config_sealed TO vektra_assess_api;

GRANT INSERT ON TABLE public.assess_audit_events TO vektra_assess_api;
GRANT INSERT ON TABLE public.assess_audit_events TO vektra_assess_worker;
GRANT INSERT ON TABLE public.assess_audit_events TO vektra_assess_definer;

GRANT USAGE ON SEQUENCE public.assess_audit_events_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_audit_events_id_seq TO vektra_assess_worker;
GRANT USAGE ON SEQUENCE public.assess_audit_events_id_seq TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_consumed_jtis TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_consumed_jtis TO vektra_assess_worker;

GRANT UPDATE(consumed_at) ON TABLE public.assess_consumed_jtis TO vektra_assess_api;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_deliveries TO vektra_assess_worker;
GRANT SELECT,INSERT ON TABLE public.assess_deliveries TO vektra_assess_definer;

GRANT SELECT(id) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(transcript_id) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(kind) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(round) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(recipient_kind) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(recipient_userid) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(status) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(attempts) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(not_before) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(attempt_started_at) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(last_error_class) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(smtp_code) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(sent_at) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT(created_at) ON TABLE public.assess_deliveries TO vektra_assess_api;

GRANT SELECT ON TABLE public.assess_enrolment_checks TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE ON TABLE public.assess_enrolment_checks TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_enrolment_checks_id_seq TO vektra_assess_worker;

GRANT SELECT,INSERT ON TABLE public.assess_erasure_records TO vektra_assess_api;
GRANT SELECT,INSERT,UPDATE ON TABLE public.assess_erasure_records TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_erasure_records_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_erasure_records_id_seq TO vektra_assess_worker;

GRANT SELECT,INSERT,UPDATE ON TABLE public.assess_extra_time TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_extra_time TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_gate_inputs TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_gate_inputs TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_heartbeat TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_heartbeat TO vektra_assess_worker;
GRANT SELECT ON TABLE public.assess_heartbeat TO vektra_assess_definer;

GRANT INSERT ON TABLE public.assess_jobs TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_jobs TO vektra_assess_worker;
GRANT SELECT,INSERT ON TABLE public.assess_jobs TO vektra_assess_definer;

GRANT USAGE ON SEQUENCE public.assess_jobs_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_jobs_id_seq TO vektra_assess_worker;
GRANT USAGE ON SEQUENCE public.assess_jobs_id_seq TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_model_activations TO vektra_assess_api;
GRANT SELECT ON TABLE public.assess_model_activations TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_model_activations_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_model_activations_id_seq TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_outage_incidents TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_outage_incidents TO vektra_assess_worker;
GRANT SELECT,INSERT ON TABLE public.assess_outage_incidents TO vektra_assess_definer;

GRANT UPDATE(ended_at) ON TABLE public.assess_outage_incidents TO vektra_assess_definer;

GRANT UPDATE(credited_s) ON TABLE public.assess_outage_incidents TO vektra_assess_definer;

GRANT USAGE ON SEQUENCE public.assess_outage_incidents_id_seq TO vektra_assess_worker;
GRANT USAGE ON SEQUENCE public.assess_outage_incidents_id_seq TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_privacy_config TO vektra_assess_api;
GRANT SELECT ON TABLE public.assess_privacy_config TO vektra_assess_worker;

GRANT SELECT(site_id) ON TABLE public.assess_privacy_config TO vektra_assess_definer;

GRANT UPDATE(retention_days) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(legal_basis_label) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(legal_basis_text) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(notice_url) ON TABLE public.assess_privacy_config TO vektra_assess_api;
GRANT SELECT(notice_url) ON TABLE public.assess_privacy_config TO vektra_assess_definer;

GRANT UPDATE(notice_text) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(notice_version) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(updated_by) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT UPDATE(updated_at) ON TABLE public.assess_privacy_config TO vektra_assess_api;

GRANT SELECT ON TABLE public.assess_purge_records TO vektra_assess_api;
GRANT SELECT,INSERT ON TABLE public.assess_purge_records TO vektra_assess_worker;

GRANT UPDATE(subject_ref) ON TABLE public.assess_purge_records TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_purge_records_id_seq TO vektra_assess_worker;

GRANT SELECT,INSERT ON TABLE public.assess_render_checks TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_render_checks TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_render_checks_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_render_checks_id_seq TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_render_state TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_render_state TO vektra_assess_worker;

GRANT SELECT,INSERT ON TABLE public.assess_reset_grants TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_reset_grants TO vektra_assess_worker;

GRANT UPDATE(consumed_at) ON TABLE public.assess_reset_grants TO vektra_assess_worker;

GRANT UPDATE(reason_ct) ON TABLE public.assess_reset_grants TO vektra_assess_worker;

GRANT UPDATE(reason_kid) ON TABLE public.assess_reset_grants TO vektra_assess_worker;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_session_handoff TO vektra_assess_worker;
GRANT SELECT,DELETE ON TABLE public.assess_session_handoff TO vektra_assess_definer;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_session_keys TO vektra_assess_worker;

GRANT SELECT(session_id) ON TABLE public.assess_session_keys TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_sessions TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_sessions TO vektra_assess_worker;
GRANT SELECT,DELETE ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(state) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(close_reason) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(deadline_at) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(credited_outage_s) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(seq) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(epoch) ON TABLE public.assess_sessions TO vektra_assess_api;
GRANT UPDATE(epoch) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(closed_at) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(last_activity_at) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(pending_reply_since) ON TABLE public.assess_sessions TO vektra_assess_definer;

GRANT UPDATE(tokens_used) ON TABLE public.assess_sessions TO vektra_assess_api;

GRANT SELECT,INSERT ON TABLE public.assess_setup_check_reports TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_setup_check_reports TO vektra_assess_worker;

GRANT SELECT,INSERT,DELETE ON TABLE public.assess_setup_checks TO vektra_assess_worker;

GRANT USAGE ON SEQUENCE public.assess_setup_checks_id_seq TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_site_capabilities TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_site_capabilities TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_site_keys TO vektra_assess_api;
GRANT SELECT ON TABLE public.assess_site_keys TO vektra_assess_worker;

GRANT SELECT(site_id) ON TABLE public.assess_site_keys TO vektra_assess_definer;

GRANT SELECT(api_key_id) ON TABLE public.assess_site_keys TO vektra_assess_definer;

GRANT SELECT(purpose) ON TABLE public.assess_site_keys TO vektra_assess_definer;

GRANT SELECT ON TABLE public.assess_sites TO vektra_assess_worker;

GRANT SELECT(id) ON TABLE public.assess_sites TO vektra_assess_api;
GRANT SELECT(id) ON TABLE public.assess_sites TO vektra_assess_definer;

GRANT SELECT(lms_site_uuid) ON TABLE public.assess_sites TO vektra_assess_api;

GRANT SELECT(base_url) ON TABLE public.assess_sites TO vektra_assess_api;

GRANT SELECT(release) ON TABLE public.assess_sites TO vektra_assess_api;
GRANT UPDATE(release) ON TABLE public.assess_sites TO vektra_assess_worker;

GRANT SELECT(min_plugin_version) ON TABLE public.assess_sites TO vektra_assess_api;
GRANT UPDATE(min_plugin_version) ON TABLE public.assess_sites TO vektra_assess_worker;
GRANT SELECT(min_plugin_version) ON TABLE public.assess_sites TO vektra_assess_definer;

GRANT SELECT(created_at) ON TABLE public.assess_sites TO vektra_assess_api;

GRANT SELECT(disabled_at) ON TABLE public.assess_sites TO vektra_assess_api;
GRANT SELECT(disabled_at) ON TABLE public.assess_sites TO vektra_assess_definer;

GRANT UPDATE(probe_manifest) ON TABLE public.assess_sites TO vektra_assess_worker;

GRANT SELECT(last_seen_release) ON TABLE public.assess_sites TO vektra_assess_api;
GRANT UPDATE(last_seen_release) ON TABLE public.assess_sites TO vektra_assess_worker;

GRANT SELECT(lms_origin) ON TABLE public.assess_sites TO vektra_assess_api;

GRANT SELECT,INSERT,DELETE ON TABLE public.assess_snapshots TO vektra_assess_worker;
GRANT SELECT ON TABLE public.assess_snapshots TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_start_attempts TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE ON TABLE public.assess_start_attempts TO vektra_assess_worker;
GRANT SELECT,INSERT ON TABLE public.assess_start_attempts TO vektra_assess_definer;

GRANT USAGE ON SEQUENCE public.assess_start_attempts_id_seq TO vektra_assess_api;
GRANT USAGE ON SEQUENCE public.assess_start_attempts_id_seq TO vektra_assess_worker;
GRANT USAGE ON SEQUENCE public.assess_start_attempts_id_seq TO vektra_assess_definer;

GRANT SELECT ON TABLE public.assess_start_breaker TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_start_breaker TO vektra_assess_worker;

GRANT SELECT ON TABLE public.assess_teacher_gate TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_teacher_gate TO vektra_assess_worker;

GRANT SELECT,INSERT ON TABLE public.assess_terminations TO vektra_assess_api;
GRANT SELECT ON TABLE public.assess_terminations TO vektra_assess_worker;

GRANT UPDATE(reason_ct) ON TABLE public.assess_terminations TO vektra_assess_worker;

GRANT UPDATE(reason_kid) ON TABLE public.assess_terminations TO vektra_assess_worker;

GRANT SELECT,INSERT ON TABLE public.assess_token_jti TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_token_jti TO vektra_assess_worker;

GRANT UPDATE(consumed_at) ON TABLE public.assess_token_jti TO vektra_assess_api;

GRANT SELECT,INSERT ON TABLE public.assess_transcript_download_tokens TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_transcript_download_tokens TO vektra_assess_worker;

GRANT UPDATE(consumed_at) ON TABLE public.assess_transcript_download_tokens TO vektra_assess_api;

GRANT SELECT ON TABLE public.assess_transcripts TO vektra_assess_api;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.assess_transcripts TO vektra_assess_worker;
GRANT SELECT ON TABLE public.assess_transcripts TO vektra_assess_definer;

GRANT SELECT,INSERT ON TABLE public.assess_turn_flags TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_turn_flags TO vektra_assess_worker;

GRANT INSERT ON TABLE public.assess_turn_notes TO vektra_assess_api;
GRANT SELECT,DELETE ON TABLE public.assess_turn_notes TO vektra_assess_worker;

GRANT SELECT,INSERT,DELETE ON TABLE public.assess_turns TO vektra_assess_worker;
GRANT SELECT,INSERT ON TABLE public.assess_turns TO vektra_assess_definer;

GRANT INSERT(response_id) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(namespace_id) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(steps) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(total_duration_ms) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(chunks_retrieved) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(llm_model) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(prompt_version) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(created_at) ON TABLE public.query_traces TO vektra_assess_worker;

GRANT INSERT(pipeline) ON TABLE public.query_traces TO vektra_assess_worker;

-- seed namespaces: assess-discussion Assess discussion traces (reserved) {"reserved": true}
-- seed namespaces: default Default namespace {}
