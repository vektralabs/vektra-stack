"""A bot turn answers exactly one pending student turn (plan 8 review fix 1).

``assess_append_turn`` (same signature, owner and grants, ``CREATE OR REPLACE``) refuses a bot
turn, normal or the marked interrupted one, unless a student turn is waiting: the session has
``pending_reply_since`` set and the last turn is the student's. Two drivers answering the same
turn, or ``reconcile_reply`` racing a live reply, therefore append one bot turn: the second
append returns NULL. No grant changes.

Revision ID: 0031
Revises: 0030
"""

import importlib.util
from pathlib import Path

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

APPEND_V4 = "\n    CREATE OR REPLACE FUNCTION public.assess_append_turn(\n        p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text,\n        p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text\n    ) RETURNS integer\n    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$\n\n    DECLARE\n        v_seq integer;\n        v_pending timestamptz;\n        v_deadline timestamptz;\n        v_grace integer;\n        v_last_no integer;\n        v_expected_prev text;\n        v_last_role text;\n    BEGIN\n        IF p_kind NOT IN ('normal', 'interrupted', 'system')\n           OR p_role NOT IN ('student', 'bot', 'system') THEN\n            RETURN NULL;  -- the opening turn is written by the worker, never here\n        END IF;\n        SELECT s.seq, s.pending_reply_since, s.deadline_at, s.reply_grace_s\n          INTO v_seq, v_pending, v_deadline, v_grace\n        FROM public.assess_sessions s\n        WHERE s.id = p_sid AND s.state = 'ACTIVE' AND s.epoch = p_epoch\n        FOR UPDATE;\n        IF NOT FOUND OR v_seq <> p_expected_seq THEN\n            RETURN NULL;\n        END IF;\n        -- REQ-070, on the database's wall clock: no student message from the deadline on, and\n        -- no ordinary bot reply after deadline + grace; the marked interrupted turn (no content\n        -- of the model) is still accepted so that a pending student turn is always answered\n        IF v_deadline IS NOT NULL THEN\n            IF p_role = 'student' AND pg_catalog.clock_timestamp() >= v_deadline THEN\n                RETURN NULL;\n            END IF;\n            IF p_role = 'bot' AND p_kind = 'normal' AND pg_catalog.clock_timestamp()\n               > v_deadline + pg_catalog.make_interval(secs => v_grace) THEN\n                RETURN NULL;\n            END IF;\n        END IF;\n        IF p_role = 'student' AND v_pending IS NOT NULL THEN\n            RETURN NULL;  -- a reply is still pending: one student turn at a time\n        END IF;\n        IF p_role = 'bot' AND v_pending IS NULL THEN\n            RETURN NULL;  -- no student turn is waiting: a second reply is never appended\n        END IF;\n        SELECT t.turn_no, t.turn_hash, t.role INTO v_last_no, v_expected_prev, v_last_role\n        FROM public.assess_turns t WHERE t.session_id = p_sid\n        ORDER BY t.turn_no DESC LIMIT 1;\n        IF NOT FOUND THEN\n            RETURN NULL;  -- no opening turn yet: nothing to chain onto\n        END IF;\n        IF p_role = 'bot' AND v_last_role <> 'student' THEN\n            RETURN NULL;  -- a bot turn answers exactly one student turn\n        END IF;\n        IF v_expected_prev IS DISTINCT FROM p_prev_hash THEN\n            RETURN NULL;\n        END IF;\n        INSERT INTO public.assess_turns\n            (session_id, turn_no, role, kind, ciphertext, turn_hash, prev_hash)\n        VALUES (p_sid, v_last_no + 1, p_role, p_kind, p_ciphertext, p_turn_hash,\n                p_prev_hash);\n        UPDATE public.assess_sessions\n           SET seq = seq + 1, last_activity_at = pg_catalog.now(),\n               pending_reply_since = CASE\n                   WHEN p_role = 'student' THEN pg_catalog.now()\n                   WHEN p_role = 'bot' THEN NULL\n                   ELSE pending_reply_since END\n         WHERE id = p_sid;\n        RETURN v_last_no + 1;\n    END\n    $$\n"


def _append_v3() -> str:
    path = Path(__file__).with_name("0024_assess_session_clock.py")
    spec = importlib.util.spec_from_file_location("rev_0024_append", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.APPEND_V3)


def upgrade() -> None:
    op.execute(APPEND_V4)


def downgrade() -> None:
    op.execute(_append_v3())
