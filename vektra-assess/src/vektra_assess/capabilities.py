"""Capability matrix reads: what a site's LMS can be read for (ARCH-070). DB only.

No ``httpx`` and no LMS provider here (contract (c), and an external-package contract): both
the API and the worker may import this module, and only the worker talks to the LMS. The matrix
is written by the worker's setup check, from the provider's ``probe_capabilities``
(``vektra_assess.setup``); this module only *reads* it and applies the fail-closed rules:

- a missing row, or a row older than 26 h, is ``Unreadable(not_probed)``;
- a row probed on another LMS release than the site's current one is
  ``Unreadable(release_changed)``;
- ``partial`` counts as unreadable for every gate (REQ-091): ``Unreadable(partial)``;
- only a fresh ``readable`` row on the current release is ``Readable``.

``site_readiness`` is the single signal that starts consult: the feature is available for a
site only if its latest finished setup-check report, for the CURRENT release, has every item
OK and every close-time flag row exists and is fresh.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_shared.lms import Readable, Reading, Unreadable, UnreadableReason

STALE_AFTER = timedelta(hours=26)

#: the closed list of capability flags (matches the CHECK of revision 0016). This is Vektra's
#: contract with every LMS provider (ADR-0033 D3): the disclosure policy and the gates consume
#: these flags, so their names and meaning are Vektra's; a provider only probes them
#: (``LmsProvider.probe_capabilities``), and a flag it does not return is ``not_probed``.
FLAGS: tuple[str, ...] = (
    "quiz_overrides", "assign_extensions", "assign_cutoff", "quiz_grace", "attempt_state",
    "per_student_timelimit", "review_html_filterable", "rubric_display_settings",
    "module_visibility_per_student", "loginas_crosscheck", "graders_resolvable",
    "enrolment_readable", "release_state", "grading_state",
    "assign_user_overrides", "review_options_per_student",
)  # fmt: skip

#: flags whose freshness gates a Start: the inputs of the effective close and attempt state.
CLOSE_TIME_FLAGS: tuple[str, ...] = (
    "quiz_overrides",
    "assign_extensions",
    "assign_cutoff",
    "quiz_grace",
    "attempt_state",
    "assign_user_overrides",
)


@dataclass(frozen=True)
class CapabilityRow:
    flag: str
    state: str  # readable | partial | unreadable
    release: str | None
    reason_code: str | None
    evidence_hash: str | None
    checked_at: datetime


def _reason(code: str | None) -> UnreadableReason:
    try:
        return UnreadableReason(code) if code else UnreadableReason.NOT_PROBED
    except ValueError:
        return UnreadableReason.NOT_PROBED


def classify(
    row: CapabilityRow | None, site_release: str | None, now: datetime
) -> Reading[CapabilityRow]:
    """The fail-closed decision for one flag. Pure: no I/O."""
    if row is None:
        return Unreadable(UnreadableReason.NOT_PROBED)
    if now - row.checked_at > STALE_AFTER:
        return Unreadable(UnreadableReason.NOT_PROBED, "stale")
    if site_release is None or row.release != site_release:
        return Unreadable(UnreadableReason.RELEASE_CHANGED)
    if row.state == "readable":
        return Readable(row)
    if row.state == "partial":
        return Unreadable(
            UnreadableReason.PARTIAL
        )  # partial is unreadable for every gate
    return Unreadable(_reason(row.reason_code))


@dataclass(frozen=True)
class Ready:
    pass


@dataclass(frozen=True)
class NotReady:
    items: tuple[str, ...]  # names of what is missing: content-free


type Readiness = Ready | NotReady


#: an unreadable row is a legitimate probed answer only for these reasons: the function does
#: not exist on that LMS version (quiz overrides on an older release). Anything else
#: (a probe that mismatched, an access error, a timeout...) means the probe could not do its
#: job, and the site is not ready. ``partial`` (the quiz grace period of a closed quiz) is an
#: expected, documented answer and is handled per gate by REQ-091.
_BY_DESIGN_UNREADABLE = frozenset({"function_not_in_service"})


def _probe_failed(row: CapabilityRow) -> bool:
    return row.state == "unreadable" and row.reason_code not in _BY_DESIGN_UNREADABLE


def evaluate_readiness(
    *,
    site_found: bool,
    disabled: bool,
    last_seen_release: str | None,
    report_items: Sequence[dict[str, Any]] | None,
    rows: dict[str, CapabilityRow],
    now: datetime,
) -> Readiness:
    """Ready only with a finished report for the current release, every item OK, and every
    close-time flag row present, fresh and probed on the current release. Pure."""
    if not site_found or disabled:
        return NotReady(("site_unavailable",))
    if last_seen_release is None:
        return NotReady(("never_checked",))
    if report_items is None:
        return NotReady(("no_report_for_current_release",))
    missing = [
        str(item.get("name", "unnamed"))
        for item in report_items
        if item.get("result") != "OK"
    ]
    for flag in CLOSE_TIME_FLAGS:
        row = rows.get(flag)
        if (
            row is None
            or now - row.checked_at > STALE_AFTER
            or row.release != last_seen_release
            or _probe_failed(row)
        ):
            missing.append(f"capability:{flag}")
    return NotReady(tuple(missing)) if missing else Ready()


async def _site_release(
    conn: AsyncConnection, site_id: Any
) -> tuple[bool, bool, str | None]:
    row = (
        await conn.execute(
            text(
                "SELECT disabled_at IS NOT NULL, last_seen_release FROM assess_sites WHERE id = :s"
            ),
            {"s": site_id},
        )
    ).one_or_none()
    return (False, False, None) if row is None else (True, bool(row[0]), row[1])


def _row(r: Any) -> CapabilityRow:
    return CapabilityRow(
        r.flag, r.state, r.release, r.reason_code, r.evidence_hash, r.checked_at
    )


async def capability(
    conn: AsyncConnection, site_id: Any, flag: str, *, now: datetime | None = None
) -> Reading[CapabilityRow]:
    if flag not in FLAGS:
        raise ValueError(f"unknown capability flag {flag!r}")
    _found, _disabled, release = await _site_release(conn, site_id)
    r = (
        await conn.execute(
            text(
                "SELECT flag, state, release, reason_code, evidence_hash, checked_at "
                "FROM assess_site_capabilities WHERE site_id = :s AND flag = :f"
            ),
            {"s": site_id, "f": flag},
        )
    ).one_or_none()
    return classify(_row(r) if r else None, release, now or datetime.now(UTC))


async def site_readiness(
    conn: AsyncConnection, site_id: Any, *, now: datetime | None = None
) -> Readiness:
    found, disabled, release = await _site_release(conn, site_id)
    items: list[dict[str, Any]] | None = None
    if found and release is not None:
        rep = (
            await conn.execute(
                text(
                    "SELECT items FROM assess_setup_check_reports WHERE site_id = :s "
                    "AND status = 'done' AND moodle_release = :r "
                    "ORDER BY finished_at DESC NULLS LAST, created_at DESC LIMIT 1"
                ),
                {"s": site_id, "r": release},
            )
        ).one_or_none()
        items = list(rep[0]) if rep else None
    rows = {
        r.flag: _row(r)
        for r in (
            await conn.execute(
                text(
                    "SELECT flag, state, release, reason_code, evidence_hash, checked_at "
                    "FROM assess_site_capabilities WHERE site_id = :s"
                ),
                {"s": site_id},
            )
        ).all()
    }
    return evaluate_readiness(
        site_found=found,
        disabled=disabled,
        last_seen_release=release,
        report_items=items,
        rows=rows,
        now=now or datetime.now(UTC),
    )


@dataclass(frozen=True)
class ReleaseObservation:
    changed: bool
    flipped: int  # capability rows set to unreadable/release_changed


async def observe_release(
    conn: AsyncConnection, site_id: Any, observed_release: str
) -> ReleaseObservation:
    """Record the release the site reports now. The caller owns the transaction: on a change,
    everything below happens in it, so a crash leaves either all of it or none.

    First sight only records the release. A CHANGE sets every capability row of the site to
    ``unreadable/release_changed``, records the new ``last_seen_release``, enqueues one
    ``setup_check`` job (deduplicated) and writes the ``assess_site_release_changed`` audit
    event. ``site_readiness`` then blocks Starts until a fresh report exists for the new release.
    """
    prev = (
        await conn.execute(
            text("SELECT last_seen_release FROM assess_sites WHERE id = :s FOR UPDATE"),
            {"s": site_id},
        )
    ).one_or_none()
    if prev is None:
        raise LookupError("unknown site")
    last = prev[0]
    if last == observed_release:
        return ReleaseObservation(False, 0)
    await conn.execute(
        text("UPDATE assess_sites SET last_seen_release = :r WHERE id = :s"),
        {"r": observed_release, "s": site_id},
    )
    if last is None:
        return ReleaseObservation(False, 0)
    flipped = (
        await conn.execute(
            text(
                "UPDATE assess_site_capabilities SET state = 'unreadable', "
                "reason_code = 'release_changed' WHERE site_id = :s"
            ),
            {"s": site_id},
        )
    ).rowcount
    await conn.execute(
        text(
            "INSERT INTO assess_jobs (kind, args, dedupe_key) VALUES "
            "('setup_check', jsonb_build_object('site_id', CAST(:s AS text)), :k) "
            "ON CONFLICT (dedupe_key) WHERE state IN ('ready','leased') DO NOTHING"
        ),
        {"s": str(site_id), "k": f"setup_check:{site_id}"},
    )
    await conn.execute(
        text(
            "INSERT INTO assess_audit_events (site_id, event, reason_code, actor_kind, data) "
            "VALUES (:s, 'assess_site_release_changed', 'release_changed', 'worker', "
            "jsonb_build_object('rows_invalidated', CAST(:n AS integer)))"
        ),
        {"s": site_id, "n": flipped},
    )
    return ReleaseObservation(True, flipped)
