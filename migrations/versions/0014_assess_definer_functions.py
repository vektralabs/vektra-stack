"""Assess SECURITY DEFINER access boundary (plan assess-data-crypto 2.1, 2.2).

ARCH-079: the API role has NO privilege on snapshots, turns, session keys or hand-off rows.
It reaches session material only through three functions owned by the NOLOGIN role
``vektra_assess_definer``. Each is ``SECURITY DEFINER``, pinned to
``SET search_path = pg_catalog, pg_temp``, fully qualifies ``public.assess_*``, and has
EXECUTE revoked from PUBLIC (Postgres grants it to PUBLIC by default). Only then is EXECUTE
granted to the API role (and the worker for ``assess_close_session``).

- ``get_session_material(sid, epoch)``: zero rows unless the session is ACTIVE/OUTAGE with
  the same epoch as its hand-off row.
- ``assess_append_turn(...)``: row lock, state ACTIVE, epoch and seq equality, hash chain
  continuity (genesis = snapshot hash); returns the new turn number or NULL.
- ``assess_close_session(sid, epoch, reason)``: lazy close without a DELETE grant on the
  hand-off row; enqueues the ``transcript_generate`` outbox job and the audit event in the
  same transaction; idempotent on an already CLOSED session.
- a CLOSED session can never be revived (trigger), so no job can re-wrap its DEK.

The definer's own table privileges are granted here, next to the functions that need them.
The deferred opening-turn trigger and the hand-off cleanup trigger become SECURITY DEFINER
too, because they read or delete rows the calling role cannot touch.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_MATERIAL = "public.get_session_material(uuid, integer)"
SIG_APPEND = (
    "public.assess_append_turn(uuid, integer, integer, text, text, bytea, text, text)"
)
SIG_CLOSE = "public.assess_close_session(uuid, integer, text)"
REASONS = (
    "'finished', 'timed_out', 'service_failure', 'budget_exhausted', "
    "'terminated_by_teacher', 'access_revoked'"
)


def upgrade() -> None:
    for stmt in (
        f"GRANT SELECT ON assess_sessions, assess_session_handoff, assess_snapshots, "
        f"assess_turns TO {DEFINER}",
        f"GRANT INSERT ON assess_turns, assess_jobs, assess_audit_events TO {DEFINER}",
        f"GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at) "
        f"ON assess_sessions TO {DEFINER}",
        f"GRANT DELETE ON assess_session_handoff TO {DEFINER}",
        f"GRANT USAGE ON SEQUENCE assess_jobs_id_seq, assess_audit_events_id_seq "
        f"TO {DEFINER}",
    ):
        op.execute(stmt)

    op.execute("""
        CREATE FUNCTION public.get_session_material(p_sid uuid, p_epoch integer)
        RETURNS TABLE (
            sealed_dek bytea, handoff_kid text, snapshot_ciphertext bytea,
            snapshot_hash text, seq integer, turn_nos integer[], turn_roles text[],
            turn_kinds text[], turn_ciphertexts bytea[], turn_hashes text[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
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
        $$
    """)

    op.execute("""
        CREATE FUNCTION public.assess_append_turn(
            p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text,
            p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text
        ) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_seq integer;
            v_last_no integer;
            v_expected_prev text;
        BEGIN
            IF p_kind NOT IN ('normal', 'interrupted', 'system')
               OR p_role NOT IN ('student', 'bot', 'system') THEN
                RETURN NULL;  -- the opening turn is written by the worker, never here
            END IF;
            SELECT s.seq INTO v_seq FROM public.assess_sessions s
            WHERE s.id = p_sid AND s.state = 'ACTIVE' AND s.epoch = p_epoch
            FOR UPDATE;
            IF NOT FOUND OR v_seq <> p_expected_seq THEN
                RETURN NULL;
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
               SET seq = seq + 1, last_activity_at = pg_catalog.now()
             WHERE id = p_sid;
            RETURN v_last_no + 1;
        END
        $$
    """)

    op.execute(f"""
        CREATE FUNCTION public.assess_close_session(
            p_sid uuid, p_epoch integer, p_reason text
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_state text;
            v_epoch integer;
            v_site uuid;
        BEGIN
            IF p_reason NOT IN ({REASONS}) THEN
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
            UPDATE public.assess_sessions
               SET state = 'CLOSED', close_reason = p_reason, closed_at = pg_catalog.now()
             WHERE id = p_sid;
            INSERT INTO public.assess_jobs (kind, args, dedupe_key)
            VALUES ('transcript_generate',
                    pg_catalog.jsonb_build_object('session_id', p_sid),
                    'transcript_generate:' || p_sid::text);
            INSERT INTO public.assess_audit_events
                (site_id, session_id, event, reason_code, actor_kind)
            VALUES (v_site, p_sid, 'session_closed', p_reason, 'api');
            RETURN true;
        END
        $$
    """)

    for sig in (SIG_MATERIAL, SIG_APPEND, SIG_CLOSE):
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIG_MATERIAL} TO vektra_assess_api")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIG_APPEND} TO vektra_assess_api")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIG_CLOSE} TO vektra_assess_api")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIG_CLOSE} TO vektra_assess_worker")

    # Triggers that read or delete rows the calling role cannot touch.
    for fn in (
        "assess_sessions_require_opening_turn()",
        "assess_sessions_drop_handoff_on_close()",
    ):
        op.execute(f"ALTER FUNCTION public.{fn} SECURITY DEFINER")
        op.execute(f"ALTER FUNCTION public.{fn} SET search_path = pg_catalog, pg_temp")
        op.execute(f"ALTER FUNCTION public.{fn} OWNER TO {DEFINER}")
        op.execute(f"REVOKE EXECUTE ON FUNCTION public.{fn} FROM PUBLIC")

    op.execute("""
        CREATE FUNCTION public.assess_sessions_no_revival() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF OLD.state = 'CLOSED' AND NEW.state <> 'CLOSED' THEN
                RAISE EXCEPTION 'assess session % is CLOSED and cannot be reopened', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assess_sessions_no_revival
            BEFORE UPDATE OF state ON assess_sessions
            FOR EACH ROW EXECUTE FUNCTION assess_sessions_no_revival()
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_sessions) THEN
                RAISE EXCEPTION 'migration 0014 downgrade refused: assess_sessions holds '
                    'rows (exam evidence). Export and purge them first.';
            END IF;
        END
        $$
    """)
    op.execute("DROP TRIGGER trg_assess_sessions_no_revival ON assess_sessions")
    op.execute("DROP FUNCTION public.assess_sessions_no_revival()")
    for sig in (SIG_MATERIAL, SIG_APPEND, SIG_CLOSE):
        op.execute(f"DROP FUNCTION {sig}")
    for fn in (
        "assess_sessions_require_opening_turn()",
        "assess_sessions_drop_handoff_on_close()",
    ):
        op.execute(f"ALTER FUNCTION public.{fn} SECURITY INVOKER")
        op.execute(f"ALTER FUNCTION public.{fn} RESET search_path")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{fn} TO PUBLIC")
    op.execute(
        f"REVOKE ALL ON assess_sessions, assess_session_handoff, assess_snapshots, "
        f"assess_turns, assess_jobs, assess_audit_events FROM {DEFINER}"
    )
    op.execute(
        f"REVOKE ALL ON SEQUENCE assess_jobs_id_seq, assess_audit_events_id_seq "
        f"FROM {DEFINER}"
    )
