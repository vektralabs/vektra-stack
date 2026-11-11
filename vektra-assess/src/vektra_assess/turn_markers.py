"""Fixed, system-written turn texts (never LLM output).

``INTERRUPTED_REPLY`` is the bot turn appended (kind ``interrupted``) when a reply could not be
delivered: the provider failed, an internal error, or the reply was cut (REQ-070). Appending it
through ``assess_append_turn`` clears ``pending_reply_since``, so the student can write again.
The API turn driver writes it today; the grace cut (plan M3.3) and the worker's
``reconcile_reply`` (plan M3.6) write the same text. It is deliberately NOT a shipped string of
``vektra_assess.llm``: those are part of the prompt version, and this text never reaches a model
as an instruction.
"""

from __future__ import annotations

INTERRUPTED_REPLY = "[risposta interrotta]"

#: the live-reply claim (M3.6): the API's TurnDriver holds this SESSION-level advisory lock while a
#: reply is in flight; the worker's ``reconcile_reply`` takes it with ``pg_try_advisory_xact_lock``
#: and leaves the turn alone while it is held. One expression for both sides.
REPLY_CLAIM_KEY_SQL = "hashtextextended('assess_reply:' || CAST(:sid AS text), 0)"


def reply_claimed_sql(sid_sql: str) -> str:
    """SQL that is true while ANY session holds the live-reply claim of ``sid_sql`` (a column or
    expression), read from ``pg_locks`` without taking the lock: scans that pick turns to answer
    or to mark skip the claimed ones and reach the next. A bigint
    advisory key appears in ``pg_locks`` split in ``classid`` (high 32 bits) and ``objid`` (low
    32 bits) with ``objsubid = 1``."""
    return (
        "EXISTS (SELECT 1 FROM pg_catalog.pg_locks l WHERE l.locktype = 'advisory' "
        "AND l.granted AND l.objsubid = 1 AND ((l.classid::bigint << 32) | l.objid::bigint) "
        f"= hashtextextended('assess_reply:' || CAST({sid_sql} AS text), 0))"
    )
