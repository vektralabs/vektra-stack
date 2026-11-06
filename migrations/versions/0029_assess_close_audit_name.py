"""The close audit event takes the enum's name (plan assess-session-lifecycle M5.6, REQ-085).

``assess_close_session`` (same signature, owner and grants, ``CREATE OR REPLACE``) writes its audit
row as ``assess_close`` (``AssessAuditEvent.CLOSE``) instead of the plan-2 ``session_closed``, so
every lifecycle event of the enum is written under one name, whichever path writes it (Python's
``write_event`` or a SECURITY DEFINER function). Rows already written keep their name. No grant
changes.

Revision ID: 0029
Revises: 0028
"""

import importlib.util
from pathlib import Path

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

CLOSE_V4 = """
    CREATE OR REPLACE FUNCTION public.assess_close_session(
        p_sid uuid, p_epoch integer, p_reason text
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
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
    $$
"""


def _close_v3() -> str:
    path = Path(__file__).with_name("0026_assess_session_outage.py")
    spec = importlib.util.spec_from_file_location("rev_0026_close", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.CLOSE_V3)


def upgrade() -> None:
    op.execute(CLOSE_V4)


def downgrade() -> None:
    op.execute(_close_v3())
