"""The reference a tombstone or an erasure record keeps of its subject (plan 12).

``sha256("<site id>|<user id>")``: per site (a user id means nothing across sites), the same
for every row of one subject so the state can find the subject's tombstones, and no raw user id
in a record meant to outlive the subject's data. It is a lookup key, not a secret: LMS user ids
are small integers, so the reference does not hide WHICH user from someone who can read the
table; only the worker and the API can, and neither ever logs it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import text

#: what a tombstone keeps once its subject is erased: nothing that leads back to them
ERASED_REF = "erased"

#: every table that holds a subject's own rows, keyed (site_id, cmid, student_userid): the one
#: list the erasure deletes and counts and the activity listing reads (no copy can drift)
SUBJECT_TABLES: tuple[str, ...] = (
    "assess_start_attempts",
    "assess_token_jti",
    "assess_gate_inputs",
    "assess_render_state",
    "assess_render_checks",
    "assess_extra_time",
    "assess_reset_grants",
    "assess_sessions",
)

#: the user ids with any row in an activity of a site (the plugin's get_users_in_context)
ACTIVITY_SUBJECTS = text(
    "SELECT u FROM ("
    + " UNION ".join(
        f"SELECT student_userid AS u FROM {t} WHERE site_id = :s AND cmid = :c"
        for t in SUBJECT_TABLES
    )
    + ") AS t WHERE u > :after ORDER BY u LIMIT :n"
)


@dataclass(frozen=True)
class Detail:
    """A subject table whose rows are exported: ``columns`` as (column, exported name), in the
    order of ``order_by``. ``into``: the item's list they go to; ``None``: at most one row per
    activity (the primary key), flattened into the item, ``defaults`` when there is none."""

    columns: tuple[tuple[str, str], ...]
    into: str | None
    order_by: str = ""
    defaults: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Count:
    """A technical subject table: only its row count per activity is exported
    (``technical_records``), never its contents."""


#: how the subject export (``GET /privacy/subjects/{userid}/activities``) shows each table of
#: ``SUBJECT_TABLES``: every table must be declared here (a unit test fails otherwise), so a new
#: subject table cannot reach the erasure without a decision on its export
SUBJECT_EXPORT: Mapping[str, Detail | Count] = {
    "assess_start_attempts": Count(),
    "assess_token_jti": Count(),
    "assess_gate_inputs": Count(),
    "assess_render_state": Count(),
    "assess_render_checks": Count(),
    # one row per (site, cmid, student). ``granted_by`` (the grader's identity) is left out
    # until the DPO says whether it is the student's data (OQ-020)
    "assess_extra_time": Detail(
        (("extra_seconds", "extra_seconds"), ("created_at", "extra_granted_at")),
        into=None,
        defaults={"extra_seconds": 0, "extra_granted_at": None},
    ),
    # the reason (encrypted, the teacher's text) is left out: OQ-020
    "assess_reset_grants": Detail(
        (
            ("session_no", "after_session_no"),
            ("granted_at", "granted_at"),
            ("consumed_at", "consumed_at"),
        ),
        into="resets",
        order_by="granted_at, session_no",
    ),
    "assess_sessions": Detail(
        (
            ("session_no", "session_no"),
            ("state", "state"),
            ("close_reason", "close_reason"),
            ("created_at", "created_at"),
            ("deadline_at", "deadline_at"),
            ("closed_at", "closed_at"),
        ),
        into="sessions",
        order_by="session_no",
    ),
}

#: tables a schema scan finds (a subject column, or a key to a session) that are NOT exported as
#: a subject's activity rows, each with the reason. Session-linked content (``privacy.purge``'s
#: ``SESSION_TABLES``, the transcripts and their tables) is reached through the subject's
#: sessions and is not listed here.
EXPORT_EXCLUDED: Mapping[str, str] = {
    "assess_erasure_records": "the erasure request itself: its ids are cleared on completion",
    "assess_jobs": "queue entries; the erasure deletes those naming the subject, no record",
}

#: the activities of a site where a subject has any row (the plugin's get_contexts_for_userid):
#: the same tables, so what the export lists is what the subject erasure deletes, plus the
#: activities with only retention tombstones naming the subject (the erasure rewrites those)
SUBJECT_ACTIVITIES = text(
    "SELECT c FROM ("
    + " UNION ".join(
        [
            f"SELECT cmid AS c FROM {t} WHERE site_id = :s AND student_userid = :u"
            for t in SUBJECT_TABLES
        ]
        + [
            "SELECT cmid AS c FROM assess_purge_records WHERE site_id = :s AND subject_ref = :ref"
        ]
    )
    + ") AS t WHERE c > :after ORDER BY c LIMIT :n"
)
#: the retention tombstones naming the subject, per activity
SUBJECT_PURGED = text(
    "SELECT cmid, count(*) AS n FROM assess_purge_records WHERE site_id = :s "
    "AND subject_ref = :ref AND cmid = ANY(:c) GROUP BY cmid"
)


def export_query(table: str) -> str:
    """The page query of one declared table: ``cmid`` first, then the declared columns (a
    ``Count`` table: its row count per activity)."""
    how = SUBJECT_EXPORT[table]
    where = "site_id = :s AND student_userid = :u AND cmid = ANY(:c)"
    if isinstance(how, Count):
        return f"SELECT cmid, count(*) AS n FROM {table} WHERE {where} GROUP BY cmid"
    cols = ", ".join(c for c, _ in how.columns)
    order = f"cmid, {how.order_by}" if how.order_by else "cmid"
    return f"SELECT cmid, {cols} FROM {table} WHERE {where} ORDER BY {order}"


def subject_ref(site_id: object, userid: int) -> str:
    return hashlib.sha256(f"{site_id}|{int(userid)}".encode("ascii")).hexdigest()
