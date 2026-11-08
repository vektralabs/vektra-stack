"""Teacher configuration sealed to the worker's public key (OQ-034 default, ARCH-071).

The API can WRITE teacher config (it holds the worker's PUBLIC key) but can never read it:
it has no private key, and ``assess_activity_config_sealed.sealed_blob`` (and ``refs``) are
outside its SELECT grant.

Storage (revision 0036, plan assess-teacher-ops 11a): one row per secret FIELD in
``assess_activity_config_sealed(site_id, cmid, field, ...)``, each sealed separately under the
binding ``[site_id, cmid, field]``. One composite blob could not work: a PUT changes one field
(absent = unchanged, null = clear, string = replace) and the API cannot read the others back to
merge them. A cleared field keeps its row with ``sealed_blob`` NULL (``is_set`` false), so the
row's last writer (``updated_by``) is what the database's change audit reports.

- ``store_sealed_field``: API side, seals and upserts one field (or clears it).
- ``read_teacher_config``: worker side, opens the fields it is asked for, one composed view.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text

from vektra_assess.crypto.envelope import Purpose
from vektra_assess.crypto.seal import open_sealed, seal
from vektra_assess.keys import AssessKeyring

SOLUTION = "solution"
PASTED_RUBRIC = "pasted_rubric"
TRANSCRIPT_RECIPIENTS = "transcript_recipients"
#: every sealed field (``ck_assess_activity_config_sealed_field``)
FIELDS: tuple[str, ...] = (SOLUTION, PASTED_RUBRIC, TRANSCRIPT_RECIPIENTS)


def _binding(site_id: object, cmid: int, field: str) -> list[object]:
    return [site_id, cmid, field]


def seal_teacher_config(
    worker_public: AssessKeyring,
    site_id: object,
    cmid: int,
    field: str,
    text_: str,
) -> tuple[bytes, str]:
    """API side. Returns ``(sealed_blob, kid)`` for one field."""
    kid = worker_public.active
    blob = seal(
        worker_public.active_key,
        kid,
        text_.encode("utf-8"),
        purpose=Purpose.TEACHER_CONFIG,
        binding=_binding(site_id, cmid, field),
    )
    return blob, kid


def open_teacher_config(
    worker_private: AssessKeyring,
    blob: bytes,
    site_id: object,
    cmid: int,
    field: str,
) -> str:
    """Worker side: one field's plaintext."""
    return open_sealed(
        worker_private,
        blob,
        purpose=Purpose.TEACHER_CONFIG,
        binding=_binding(site_id, cmid, field),
    ).decode("utf-8")


class Conn(Protocol):
    async def execute(self, statement: Any, parameters: Any = ..., /) -> Any: ...


# The API never names ``sealed_blob`` / ``refs`` / ``kid`` where they would be READ (a WHERE,
# RETURNING, SELECT, or ``EXCLUDED.col``: Postgres checks ``EXCLUDED`` references against the
# target's SELECT privilege, verified on pg16), so the DO UPDATE sets the bound parameters again.
_UPSERT = text(
    "INSERT INTO assess_activity_config_sealed (site_id, cmid, field, sealed_blob, kid, refs, "
    "item_count, updated_by, updated_at) VALUES (:s, :c, :f, :b, :k, :r, :n, :u, now()) "
    "ON CONFLICT (site_id, cmid, field) DO UPDATE SET sealed_blob = :b, kid = :k, refs = :r, "
    "item_count = :n, updated_by = :u, updated_at = now()"
)


async def store_sealed_field(
    conn: Conn,
    worker_public: AssessKeyring,
    site_id: object,
    cmid: int,
    field: str,
    plaintext: str | None,
    *,
    actor_userid: int,
    refs: Sequence[str] | None = None,
    item_count: int | None = None,
) -> None:
    """Seal ``plaintext`` for ``field`` and upsert it; ``None`` clears the field (the row stays,
    with no blob). ``refs`` / ``item_count``: the recipient refs (sorted) and how many there are,
    for ``transcript_recipients`` only: the database's change audit records old/new refs from
    them, the API never reads them back."""
    if field not in FIELDS:
        raise ValueError(f"unknown sealed field {field!r}")
    blob = kid = None
    if plaintext is not None:
        blob, kid = seal_teacher_config(worker_public, site_id, cmid, field, plaintext)
    await conn.execute(
        _UPSERT,
        {
            "s": site_id,
            "c": int(cmid),
            "f": field,
            "b": blob,
            "k": kid,
            "r": sorted(refs) if (refs is not None and plaintext is not None) else None,
            "n": item_count if plaintext is not None else None,
            "u": int(actor_userid),
        },
    )


@dataclass(frozen=True)
class TeacherSecrets:
    """The opened fields of one activity (worker side). A field that is unset, or was not asked
    for, is None; one whose seal does not open, or whose plaintext has the wrong shape, is None
    AND named in ``unreadable`` (never guessed)."""

    solution: str | None = None
    pasted_rubric: str | None = None
    transcript_recipients: tuple[str, ...] | None = None
    unreadable: frozenset[str] = frozenset()


_READ = text(
    "SELECT field, sealed_blob FROM assess_activity_config_sealed "
    "WHERE site_id = :s AND cmid = :c AND sealed_blob IS NOT NULL AND field = ANY(:f)"
)


async def read_teacher_config(
    conn: Conn,
    worker_private: AssessKeyring,
    site_id: object,
    cmid: int,
    fields: Iterable[str] = FIELDS,
) -> TeacherSecrets:
    """Open the requested sealed fields of (site, cmid). Only the fields asked for are fetched:
    a field the caller must not see (the solution with "mostra soluzione" OFF) is never read."""
    wanted = [f for f in fields if f in FIELDS]
    if not wanted:
        return TeacherSecrets()
    rows = (
        await conn.execute(_READ, {"s": site_id, "c": int(cmid), "f": wanted})
    ).all()
    values: dict[str, Any] = {}
    bad: set[str] = set()
    for field, blob in rows:
        try:
            plain = open_teacher_config(
                worker_private, bytes(blob), site_id, int(cmid), field
            )
        except (
            Exception
        ):  # InvalidTag, unknown kid, not UTF-8: unreadable, never guessed
            bad.add(field)
            continue
        if field == TRANSCRIPT_RECIPIENTS:
            try:
                doc = json.loads(plain)
            except (
                json.JSONDecodeError,
                RecursionError,
            ):  # nesting past the decoder's depth
                doc = None
            if not isinstance(doc, list) or not all(isinstance(a, str) for a in doc):
                bad.add(field)
                continue
            values[field] = tuple(doc)
        elif plain:
            values[field] = plain
    return TeacherSecrets(
        solution=values.get(SOLUTION),
        pasted_rubric=values.get(PASTED_RUBRIC),
        transcript_recipients=values.get(TRANSCRIPT_RECIPIENTS),
        unreadable=frozenset(bad),
    )
