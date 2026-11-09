"""Teacher operations: reset, terminate, the reset notice (plan assess-teacher-ops 11b).

- ``assess_reset_grants`` (revision 0010, read by the START gates since plan 8, never written
  before this revision) becomes the REQ-087 reset record: ``session_no`` (the CLOSED session the
  reset follows; UNIQUE per student and activity, so two concurrent resets of the same session
  yield one), ``reason_ct`` / ``reason_kid`` (the teacher's reason, encrypted under the transcript
  KEK: readable by the API for the grader list, never in the audit table), and
  ``include_previous_turns`` (the continuity choice, REQ-087 (b)). ``granted_by`` becomes
  mandatory. The API loses UPDATE on the table: a reset is never rewritten. The upgrade refuses
  a table with rows (none was ever written: nothing could set the new mandatory columns).
- ``assess_terminations``: one row per REQ-081 terminate (reason encrypted the same way, the
  counts it closed and rolled back, whether it also disabled the activity). Audit rows carry only
  its id.
- ``assess_extra_time``: 0 seconds is a valid grant (BR-013: 0-240 min / 0-200 %, a teacher
  setting it back to none): ``ck_assess_extra_time_positive`` (> 0) becomes
  ``ck_assess_extra_time_nonnegative`` (>= 0).
- No REQ-092 notice path: the reset notice is out of v1 (product owner 2026-11-08), its flag is
  refused at startup, and nothing here can queue a ``reset_notice`` row.
- ``assess_rollback_starting(uuid)``: SECURITY DEFINER, EXECUTE for the API only. Deletes ONE
  session that is still STARTING (pre-flight not committed: nothing consumed, the student is
  ELIGIBLE again), verifies by count that nothing of it is left, records the ``rolled_back``
  attempt with reason ``terminated_by_teacher`` (not a system rollback: it counts towards neither
  the abuse cap nor the Start breaker) and the ``assess_start_rollback`` audit row. Any other
  state: false, nothing touched.

Revision ID: 0038
Revises: 0037
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

DEFINER = "vektra_assess_definer"
SIG_ROLLBACK = "public.assess_rollback_starting(uuid)"

#: revision 0036's frozen matrix plus this revision's changes, in the rendered order (the
#: integration test compares the result with ``vektra_assess.db.grants.render_statements``).
#: Written as a delta so this file repeats none of 0036's statements.
_DROP = ("GRANT UPDATE ON assess_reset_grants TO vektra_assess_api",)
_REPLACE = {
    "GRANT UPDATE (consumed_at) ON assess_reset_grants TO vektra_assess_worker": (
        "GRANT UPDATE (consumed_at, reason_ct, reason_kid) ON assess_reset_grants TO "
        "vektra_assess_worker"
    ),
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, "
    "assess_outage_incidents_id_seq TO vektra_assess_definer": (
        "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, "
        "assess_outage_incidents_id_seq, assess_start_attempts_id_seq TO vektra_assess_definer"
    ),
}
#: (the statement it follows, the new statements)
_INSERT_AFTER = (
    (
        "GRANT SELECT ON assess_teacher_gate TO vektra_assess_api",
        (
            "GRANT INSERT ON assess_terminations TO vektra_assess_api",
            "GRANT SELECT ON assess_terminations TO vektra_assess_api",
        ),
    ),
    (
        "GRANT UPDATE ON assess_teacher_gate TO vektra_assess_worker",
        (
            "GRANT SELECT ON assess_terminations TO vektra_assess_worker",
            "GRANT UPDATE (reason_ct, reason_kid) ON assess_terminations TO "
            "vektra_assess_worker",
        ),
    ),
    (
        "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
        (
            "GRANT SELECT (session_id) ON assess_session_keys TO vektra_assess_definer",
            "GRANT DELETE ON assess_sessions TO vektra_assess_definer",
        ),
    ),
    (
        "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
        ("GRANT INSERT ON assess_start_attempts TO vektra_assess_definer",),
    ),
    (
        "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with "
        "time zone,text) TO vektra_assess_api",
        (
            "REVOKE ALL ON FUNCTION public.assess_rollback_starting(uuid) FROM PUBLIC, "
            "vektra_assess_api, vektra_assess_worker",
            "GRANT EXECUTE ON FUNCTION public.assess_rollback_starting(uuid) TO "
            "vektra_assess_api",
        ),
    ),
)


def _rev(name: str):  # type: ignore[no-untyped-def]
    from importlib import util
    from pathlib import Path

    spec = util.spec_from_file_location(
        f"rev_{name[:4]}", Path(__file__).with_name(name)
    )
    assert spec is not None and spec.loader is not None
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _statements() -> tuple[str, ...]:
    out: list[str] = []
    after = dict(_INSERT_AFTER)
    for stmt in _rev("0036_assess_teacher_config.py").GRANT_STATEMENTS:
        if stmt in _DROP:
            continue
        if stmt.startswith("REVOKE ALL ON assess_activity_config,"):
            # the new table joins the REVOKE list in its sorted place
            stmt = stmt.replace(
                "assess_teacher_gate, assess_token_jti",
                "assess_teacher_gate, assess_terminations, assess_token_jti",
            )
        out.append(_REPLACE.get(stmt, stmt))
        out.extend(after.get(stmt, ()))
    return tuple(out)


GRANT_STATEMENTS = (*_statements(),)

ROLLBACK_FN = """
    CREATE FUNCTION public.assess_rollback_starting(p_sid uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
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
    $$
"""


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM assess_reset_grants) THEN
                RAISE EXCEPTION 'assess_reset_grants holds rows that no release could write: '
                    'they have no reason, session number or continuity choice';
            END IF;
        END
        $$
    """)
    op.execute("ALTER TABLE assess_reset_grants ALTER COLUMN granted_by SET NOT NULL")
    op.execute(
        "ALTER TABLE assess_reset_grants ADD COLUMN session_no INTEGER NOT NULL "
        "CONSTRAINT ck_assess_reset_grants_session_no CHECK (session_no >= 1)"
    )
    op.execute("ALTER TABLE assess_reset_grants ADD COLUMN reason_ct BYTEA NOT NULL")
    op.execute("ALTER TABLE assess_reset_grants ADD COLUMN reason_kid TEXT NOT NULL")
    op.execute(
        "ALTER TABLE assess_reset_grants ADD COLUMN include_previous_turns BOOLEAN NOT NULL "
        "DEFAULT false"
    )
    op.execute(
        "ALTER TABLE assess_reset_grants ADD CONSTRAINT uq_assess_reset_grants_session "
        "UNIQUE (site_id, cmid, student_userid, session_no)"
    )
    op.execute("""
        CREATE TABLE assess_terminations (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            site_id            UUID NOT NULL REFERENCES assess_sites(id) ON DELETE CASCADE,
            cmid               BIGINT NOT NULL,
            reason_ct          BYTEA NOT NULL,
            reason_kid         TEXT NOT NULL,
            closed_count       INTEGER NOT NULL CHECK (closed_count >= 0),
            rolled_back_count  INTEGER NOT NULL CHECK (rolled_back_count >= 0),
            also_disable       BOOLEAN NOT NULL,
            created_by         TEXT NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_assess_terminations_activity ON assess_terminations (site_id, cmid)"
    )
    op.execute(
        "ALTER TABLE assess_extra_time DROP CONSTRAINT ck_assess_extra_time_positive"
    )
    op.execute(
        "ALTER TABLE assess_extra_time ADD CONSTRAINT ck_assess_extra_time_nonnegative "
        "CHECK (extra_seconds >= 0)"
    )
    op.execute(ROLLBACK_FN)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema (revoked again by the matrix below)
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    op.execute(f"ALTER FUNCTION {SIG_ROLLBACK} OWNER TO {DEFINER}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SIG_ROLLBACK} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIG_ROLLBACK}")
    # a zero grant is "none" under the old constraint
    op.execute("DELETE FROM assess_extra_time WHERE extra_seconds = 0")
    op.execute(
        "ALTER TABLE assess_extra_time DROP CONSTRAINT ck_assess_extra_time_nonnegative"
    )
    op.execute(
        "ALTER TABLE assess_extra_time ADD CONSTRAINT ck_assess_extra_time_positive "
        "CHECK (extra_seconds > 0)"
    )
    # the resets written by this revision cannot exist without its columns
    op.execute("DROP TABLE assess_terminations")
    op.execute("DELETE FROM assess_reset_grants")
    op.execute(
        "ALTER TABLE assess_reset_grants DROP CONSTRAINT uq_assess_reset_grants_session"
    )
    for column in ("include_previous_turns", "reason_kid", "reason_ct", "session_no"):
        op.execute(f"ALTER TABLE assess_reset_grants DROP COLUMN {column}")
    op.execute("ALTER TABLE assess_reset_grants ALTER COLUMN granted_by DROP NOT NULL")
    for stmt in _rev("0036_assess_teacher_config.py").GRANT_STATEMENTS:
        op.execute(stmt)
