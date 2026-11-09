"""The reference a tombstone or an erasure record keeps of its subject (plan 12).

``sha256("<site id>|<user id>")``: per site (a user id means nothing across sites), the same
for every row of one subject so the state can find the subject's tombstones, and no raw user id
in a record meant to outlive the subject's data. It is a lookup key, not a secret: LMS user ids
are small integers, so the reference does not hide WHICH user from someone who can read the
table; only the worker and the API can, and neither ever logs it.
"""

from __future__ import annotations

import hashlib


def subject_ref(site_id: object, userid: int) -> str:
    return hashlib.sha256(f"{site_id}|{int(userid)}".encode("ascii")).hexdigest()
