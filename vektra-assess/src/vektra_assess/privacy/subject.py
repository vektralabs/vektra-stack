"""The reference a tombstone or an erasure record keeps of its subject (plan 12).

``sha256("<site id>|<user id>")``: per site (a user id means nothing across sites), the same
for every row of one subject so the state can find the subject's tombstones, and no raw user id
in a record meant to outlive the subject's data. It is a lookup key, not a secret: LMS user ids
are small integers, so the reference does not hide WHICH user from someone who can read the
table; only the worker and the API can, and neither ever logs it.
"""

from __future__ import annotations

import hashlib

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

#: the activities of a site where a subject has any row (the plugin's get_contexts_for_userid):
#: the same tables, so what the export lists is what the subject erasure deletes
SUBJECT_ACTIVITIES = text(
    "SELECT c FROM ("
    + " UNION ".join(
        f"SELECT cmid AS c FROM {t} WHERE site_id = :s AND student_userid = :u"
        for t in SUBJECT_TABLES
    )
    + ") AS t WHERE c > :after ORDER BY c LIMIT :n"
)


def subject_ref(site_id: object, userid: int) -> str:
    return hashlib.sha256(f"{site_id}|{int(userid)}".encode("ascii")).hexdigest()
