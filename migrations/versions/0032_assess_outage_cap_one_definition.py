"""One definition of the outage cap C and of the spent outage (plan 8 review fix 10).

- ``assess_outage_cap(duration_s)``: C = min(900 s, duration / 2). A NULL duration gives 0: a
  committed session always has one (the commit guard), so NULL means inconsistent data, and the
  prudent reading is no credit and an immediate ``service_failure`` rather than a session left in
  OUTAGE with no bound.
- ``assess_outage_spent(sid)``: the seconds of every interval of the session, an open one up to
  ``now()``.

Both are plain SQL functions (SECURITY INVOKER, pinned search_path): they read only what the
caller may read. ``assess_end_outage`` (same signature, owner and grants), the worker's probe tick
and the API's lazy close use them; ``vektra_assess.timing.outage_cap_s`` mirrors the cap for
Python and a test checks the two agree. No grant changes.

Revision ID: 0032
Revises: 0031
"""

import importlib.util
from pathlib import Path

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

CAP = """
    CREATE FUNCTION public.assess_outage_cap(p_duration_s integer) RETURNS integer
    LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
        SELECT least(900, coalesce(p_duration_s, 0) / 2)
    $$
"""

SPENT = """
    CREATE FUNCTION public.assess_outage_spent(p_sid uuid) RETURNS double precision
    LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
        SELECT coalesce(sum(extract(epoch FROM coalesce(i.ended_at, now()) - i.started_at)),
                        0)::double precision
        FROM public.assess_outage_incidents i WHERE i.session_id = p_sid
    $$
"""

END_V2 = "\n    CREATE OR REPLACE FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer)\n    RETURNS integer\n    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$\n    DECLARE\n        v_state text;\n        v_epoch integer;\n        v_site uuid;\n        v_duration integer;\n        v_credited integer;\n        v_cap integer;\n        v_spent double precision;\n        v_open_id bigint;\n        v_open_s double precision;\n        v_credit integer;\n    BEGIN\n        SELECT s.state, s.epoch, s.site_id, s.duration_s, s.credited_outage_s\n          INTO v_state, v_epoch, v_site, v_duration, v_credited\n        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;\n        IF NOT FOUND OR v_state <> 'OUTAGE' THEN\n            RETURN NULL;\n        END IF;\n        -- C and the spent outage: the one definition (revision 0032)\n        v_cap := public.assess_outage_cap(v_duration);\n        v_spent := public.assess_outage_spent(p_sid);\n        IF v_spent > v_cap THEN\n            PERFORM public.assess_close_session(p_sid, v_epoch, 'service_failure');\n            RETURN -1;\n        END IF;\n        SELECT i.id, extract(epoch FROM pg_catalog.now() - i.started_at)\n          INTO v_open_id, v_open_s\n          FROM public.assess_outage_incidents i\n         WHERE i.session_id = p_sid AND i.ended_at IS NULL\n         ORDER BY i.started_at DESC LIMIT 1;\n        v_credit := 0;\n        IF v_open_id IS NOT NULL AND v_open_s >= p_min_credit_s THEN\n            v_credit := least(floor(v_open_s)::integer, greatest(v_cap - v_credited, 0));\n        END IF;\n        UPDATE public.assess_outage_incidents\n           SET ended_at = pg_catalog.now(), credited_s = v_credit\n         WHERE id = v_open_id;\n        UPDATE public.assess_sessions\n           SET state = 'ACTIVE',\n               credited_outage_s = credited_outage_s + v_credit,\n               -- DEADLINE_SQL with the new credit (SET reads the old column values)\n               deadline_at = committed_at\n                   + make_interval(secs => duration_s + credited_outage_s + v_credit),\n               pending_reply_since = CASE WHEN pending_reply_since IS NULL THEN NULL\n                                          ELSE pg_catalog.now() END\n         WHERE id = p_sid;\n        INSERT INTO public.assess_audit_events\n            (site_id, session_id, event, reason_code, actor_kind, data, created_at)\n        VALUES (v_site, p_sid, 'assess_outage_end', NULL, 'worker',\n                pg_catalog.jsonb_build_object('credited_s', v_credit),\n                pg_catalog.clock_timestamp());\n        RETURN v_credit;\n    END\n    $$\n"


def _end_v1() -> str:
    path = Path(__file__).with_name("0026_assess_session_outage.py")
    spec = importlib.util.spec_from_file_location("rev_0026_end", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.END).replace(
        "CREATE FUNCTION public.assess_end_outage",
        "CREATE OR REPLACE FUNCTION public.assess_end_outage",
    )


def upgrade() -> None:
    op.execute(CAP)
    op.execute(SPENT)
    op.execute(END_V2)


def downgrade() -> None:
    op.execute(_end_v1())
    op.execute("DROP FUNCTION public.assess_outage_spent(uuid)")
    op.execute("DROP FUNCTION public.assess_outage_cap(integer)")
