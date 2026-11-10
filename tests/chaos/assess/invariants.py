"""The single invariant checker of the assess chaos suite (plan assess-hardening-release).

Every chaos scenario ends here. The checker reads rows with the superuser connection and never an
application status field: a ``completed`` job, a ``SENT`` status or a green health endpoint are
not evidence until the rows behind them have been counted.

Invariants (each query returns the offending ids, so an empty list is the only pass):

- ``one_live_session``: at most one non-CLOSED session per (site, cmid, student);
- ``consumed_iff_opening``: a committed session (ACTIVE, OUTAGE, CLOSED) has its opening bot turn,
  and a STARTING one has none; sessions whose content a purge removed (tombstone) are exempt;
- ``handoff_only_live``: no ``assess_session_handoff`` row for a session not ACTIVE / OUTAGE;
- ``no_orphan_content``: no snapshot, key, turn or hand-off row for a session that does not exist;
- ``transcript_per_closed`` (only with ``settled=True``): every CLOSED, retained session has
  exactly one transcript, and no transcript exists for a session that is not CLOSED;
- ``one_sent_per_recipient``: per (transcript, recipient, round) at most one SENT row;
- ``sink_copies`` (only with a mail sink): every SENT row has 1 copy on the sink, or 2 when an
  audited ``assess_delivery_possible_duplicate`` names that very row (``data.delivery_id``);
- ``no_stale_lease``: no job still leased past ``lease_until`` + grace;
- ``no_stale_starting``: no STARTING row older than the reaper bound;
- ``key_present``: every committed, retained session keeps a wrapped key (no accidental shred).

False-pass guard: ``expect_sessions`` declares how many sessions the scenario created; fewer rows
than declared fails the check instead of passing on an empty table. The count is scoped to the
scenario's own ``sites`` (required whenever something is declared), so rows that an earlier test
left on a module-scoped database cannot stand in for sessions this scenario never made.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RETAINED = "s.id NOT IN (SELECT session_id FROM assess_purge_records WHERE session_id IS NOT NULL)"

QUERIES: dict[str, str] = {
    "one_live_session": (
        "SELECT site_id::text || ':' || cmid || ':' || student_userid FROM assess_sessions "
        "WHERE state <> 'CLOSED' GROUP BY site_id, cmid, student_userid HAVING count(*) > 1"
    ),
    "consumed_iff_opening": (
        "SELECT s.id::text FROM assess_sessions s WHERE "
        f"{RETAINED} AND ("
        "(s.state IN ('ACTIVE','OUTAGE','CLOSED') AND NOT EXISTS (SELECT 1 FROM assess_turns t "
        " WHERE t.session_id = s.id AND t.role = 'bot' AND t.kind = 'opening')) OR "
        "(s.state = 'STARTING' AND EXISTS (SELECT 1 FROM assess_turns t "
        " WHERE t.session_id = s.id AND t.kind = 'opening')))"
    ),
    "handoff_only_live": (
        "SELECT h.session_id::text FROM assess_session_handoff h "
        "JOIN assess_sessions s ON s.id = h.session_id WHERE s.state NOT IN ('ACTIVE','OUTAGE')"
    ),
    "no_orphan_content": (
        "SELECT 'snapshot:' || session_id FROM assess_snapshots WHERE session_id NOT IN "
        "(SELECT id FROM assess_sessions) UNION ALL "
        "SELECT 'key:' || session_id FROM assess_session_keys WHERE session_id NOT IN "
        "(SELECT id FROM assess_sessions) UNION ALL "
        "SELECT 'turn:' || session_id FROM assess_turns WHERE session_id NOT IN "
        "(SELECT id FROM assess_sessions) UNION ALL "
        "SELECT 'handoff:' || session_id FROM assess_session_handoff WHERE session_id NOT IN "
        "(SELECT id FROM assess_sessions)"
    ),
    "transcript_per_closed": (
        "SELECT s.id::text FROM assess_sessions s WHERE s.state = 'CLOSED' AND "
        f"{RETAINED} AND (SELECT count(*) FROM assess_transcripts t "
        " WHERE t.session_id = s.id) <> 1 "
        "UNION ALL SELECT 'not-closed:' || t.session_id FROM assess_transcripts t "
        "JOIN assess_sessions s ON s.id = t.session_id WHERE s.state <> 'CLOSED'"
    ),
    "one_sent_per_recipient": (
        "SELECT transcript_id::text || ':' || recipient_ref || ':' || round FROM assess_deliveries "
        "WHERE kind = 'transcript' AND status = 'SENT' "
        "GROUP BY transcript_id, recipient_ref, round HAVING count(*) > 1"
    ),
    "no_stale_lease": (
        "SELECT id::text FROM assess_jobs WHERE state = 'leased' "
        "AND lease_until < now() - make_interval(secs => :grace_s)"
    ),
    "no_stale_starting": (
        "SELECT id::text FROM assess_sessions WHERE state = 'STARTING' "
        "AND starting_expires_at < now() - make_interval(secs => :grace_s)"
    ),
    "key_present": (
        "SELECT s.id::text FROM assess_sessions s WHERE s.state IN ('ACTIVE','OUTAGE','CLOSED') "
        f"AND {RETAINED} AND NOT EXISTS (SELECT 1 FROM assess_session_keys k "
        " WHERE k.session_id = s.id AND k.kek_wrapped IS NOT NULL)"
    ),
}

ALWAYS = (
    "one_live_session",
    "consumed_iff_opening",
    "handoff_only_live",
    "no_orphan_content",
    "one_sent_per_recipient",
    "no_stale_lease",
    "no_stale_starting",
    "key_present",
)

_SENT = (
    # the duplicate audit names its own row (``data.delivery_id``): one recipient's legitimate
    # resend never excuses another recipient's second copy
    "SELECT d.transcript_id::text, d.message_id, EXISTS (SELECT 1 FROM assess_audit_events e "
    " WHERE e.event = 'assess_delivery_possible_duplicate' AND e.session_id = t.session_id "
    " AND e.data->>'delivery_id' = d.id::text) "
    "FROM assess_deliveries d JOIN assess_transcripts t ON t.id = d.transcript_id "
    "WHERE d.kind = 'transcript' AND d.status = 'SENT'"
)


@dataclass
class Report:
    scenario: str
    sessions: int
    expected_sessions: int
    violations: dict[str, list[str]] = field(default_factory=dict)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.sessions >= self.expected_sessions and not any(
            self.violations.values()
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "ok": self.ok,
            "sessions": self.sessions,
            "expected_sessions": self.expected_sessions,
            "checked": self.checked,
            "violations": {k: v for k, v in self.violations.items() if v},
        }


async def check(
    pg: Any,
    scenario: str,
    *,
    expect_sessions: int,
    settled: bool = False,
    grace_s: float = 5.0,
    sink_copies: Callable[[str], int] | None = None,
    only: Iterable[str] | None = None,
    sites: Iterable[Any] | None = None,
) -> Report:
    """Run every invariant and return the report (never raises on a violation: ``assert_ok``
    does). ``sites``: the sites the scenario created, required when ``expect_sessions > 0``."""
    names = list(only) if only is not None else list(ALWAYS)
    if settled and only is None:
        names.append("transcript_per_closed")
    scope = [str(s) for s in sites] if sites is not None else None
    if expect_sessions > 0 and not scope:
        raise ValueError(
            f"{scenario}: expect_sessions={expect_sessions} needs the scenario's sites, or "
            "sessions left by earlier tests would satisfy it"
        )
    sessions = (
        (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_sessions "
                "WHERE CAST(site_id AS text) = ANY(CAST(:sites AS text[]))",
                {"sites": scope},
            )
        )[0][0]
        if scope
        else 0
    )
    report = Report(scenario, int(sessions), expect_sessions)
    for name in names:
        rows = await pg.run("super", QUERIES[name], {"grace_s": grace_s})
        report.violations[name] = sorted(str(r[0]) for r in rows)
        report.checked.append(name)
    if sink_copies is not None:
        bad = []
        for transcript, message_id, duplicate in await pg.run("super", _SENT):
            n = sink_copies(message_id)
            if n < 1 or n > (2 if duplicate else 1):
                bad.append(f"{transcript}:{message_id}:{n}")
        report.violations["sink_copies"] = sorted(bad)
        report.checked.append("sink_copies")
    _write(report)
    return report


def assert_ok(report: Report) -> None:
    assert report.sessions >= report.expected_sessions, (
        f"{report.scenario}: {report.sessions} session rows, the scenario declared "
        f"{report.expected_sessions}: the check would pass on rows that were never created"
    )
    bad = {k: v for k, v in report.violations.items() if v}
    assert not bad, f"{report.scenario}: invariant violations {json.dumps(bad)}"


async def assert_invariants(pg: Any, scenario: str, **kw: Any) -> Report:
    report = await check(pg, scenario, **kw)
    assert_ok(report)
    return report


def _write(report: Report) -> None:
    """One JSON line per scenario into ``$CHAOS_REPORT_DIR/invariants.jsonl`` (the CI artifact)."""
    directory = os.environ.get("CHAOS_REPORT_DIR")
    if not directory:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with (path / "invariants.jsonl").open("a") as fh:
        fh.write(json.dumps(report.as_dict()) + "\n")
