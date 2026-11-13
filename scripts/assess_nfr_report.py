#!/usr/bin/env python3
"""Assess NFR campaign report from rows and audit timestamps (plan assess-hardening-release).

Numbers typed by hand are not evidence. This script reads a campaign run straight from the
database, with the superuser or a read-only role that row-level security does not restrict
(``BYPASSRLS``; see the last rule below), over a time window. It computes:

- **NFR-018 commit latency**: ``assess_sessions.created_at`` (the Start request writes the
  STARTING row) to ``committed_at``, as p50 / p95 / p99 / max. Pass: p50 <= 10 s, p95 <= 30 s and
  max < 60 s.
- **NFR-018 full reply**: a student turn's ``created_at`` to the next bot turn's ``created_at``,
  as p95. Pass: p95 <= 30 s.
- **NFR-018 first visible output**: not in any row. The stream's first token is seen only by the
  client, so this is always reported BLOCKED with the reason, never computed from a proxy.
- **NFR-019 commit rate**: committed Start attempts over admitted ones. Pass: >= 99%. Admitted
  leaves out capacity refusals (they belong to NFR-020) and rollbacks caused by the student or
  the teacher (``part_b_rejected``, ``terminated_by_teacher``). The API's advisory refusals,
  which exist only as ``assess_start_refused`` audit events, are read too: ``capacity`` goes to
  NFR-020, a worker that is not ready counts against the rate.
- **NFR-019 service_failure rate**: ``service_failure`` closes over all closes. Pass: <= 2%.
- **NFR-020 inputs**: capacity refusals, and the distribution of concurrently live sessions
  (the maximum and the time spent at each level). The K decision is made by people, not here.

False-pass guards:

- A sample below its declared minimum is BLOCKED, never a percentile over a few rows.
- A missing DSN or window is BLOCKED.
- The verdict is PASS only when every computed criterion passes. A BLOCKED item keeps the overall
  verdict at BLOCKED, never PASS.

- A role that row-level security restricts (ADR-0033 D9.2: the assess revision ``a0002`` puts
  policies on ``assess_sessions`` and ``assess_start_attempts``) would read an empty or partial
  campaign without an error. Before reading, the script asks the database whether RLS is active
  for the connecting role on those tables with no unconditional policy for it, and REFUSES (exit
  3) naming the tables. A role that sees every row: the superuser, the tables' owner, a role
  with ``BYPASSRLS``, or the worker role (its policy is unconditional).

Output: a dated markdown and JSON pair, ``tests/nfr/results/assess-nfr-<stamp>.{md,json}``.
Exit status: 0 PASS, 1 FAIL, 2 BLOCKED, 3 REFUSED (a role that cannot see every row).

Usage::

    VEKTRA_NFR_ASSESS_DSN=postgresql://... VEKTRA_NFR_ASSESS_SINCE=2026-11-10T09:00:00Z \\
    [VEKTRA_NFR_ASSESS_UNTIL=...] [VEKTRA_NFR_ASSESS_LABEL=k10-burst30] \\
    python scripts/assess_nfr_report.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "tests" / "nfr" / "results"

PASS, FAIL, BLOCKED, REFUSED = "PASS", "FAIL", "BLOCKED", "REFUSED"

#: declared minimum sample sizes (NFR-018 bursts are 20/30/40 Starts; the soak is far larger)
MIN_COMMITS = 20
MIN_REPLIES = 20
MIN_ATTEMPTS = 20
MIN_CLOSES = 20

COMMIT_P50_S, COMMIT_P95_S, COMMIT_MAX_S = 10.0, 30.0, 60.0
REPLY_P95_S = 30.0
COMMIT_RATE_MIN = 0.99
SERVICE_FAILURE_MAX = 0.02

#: rollbacks that do not count against availability (NFR-019 "excluding student-caused and
#: capacity refusals"); capacity is reported separately for NFR-020
NOT_ADMITTED = frozenset({"capacity", "part_b_rejected", "terminated_by_teacher"})

QUERY_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Config:
    dsn: str | None
    since: datetime | None
    until: datetime | None
    label: str
    invalid: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        invalid: list[str] = []

        def ts(name: str) -> datetime | None:
            raw = e.get(name)
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                invalid.append(f"{name}={raw!r} is not an ISO 8601 timestamp")
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        since, until = ts("VEKTRA_NFR_ASSESS_SINCE"), ts("VEKTRA_NFR_ASSESS_UNTIL")
        return cls(
            dsn=e.get("VEKTRA_NFR_ASSESS_DSN") or None,
            since=since,
            until=until,
            label=e.get("VEKTRA_NFR_ASSESS_LABEL", "campaign"),
            invalid=tuple(invalid),
        )

    def blockers(self) -> list[str]:
        out = list(self.invalid)
        if not self.dsn:
            out.append("VEKTRA_NFR_ASSESS_DSN is not set (the campaign database)")
        if self.since is None and not any("SINCE" in i for i in self.invalid):
            out.append(
                "VEKTRA_NFR_ASSESS_SINCE is not set (the start of the run window)"
            )
        return out


@dataclass
class Data:
    """What a run left in the database, already reduced to seconds and codes."""

    commit_s: list[float] = field(default_factory=list)
    reply_s: list[float] = field(default_factory=list)
    attempts: list[tuple[str, str | None]] = field(default_factory=list)
    close_reasons: list[str] = field(default_factory=list)
    live_intervals: list[tuple[float, float]] = field(default_factory=list)


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; None on an empty set."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def concurrency(intervals: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Maximum concurrently live sessions and seconds spent at each level (sweep line)."""
    events = sorted(
        [(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
        key=lambda e: (e[0], e[1]),  # an end before a start at the same instant
    )
    level, peak, last = 0, 0, None
    seconds: dict[int, float] = {}
    for t, delta in events:
        if last is not None and level > 0:
            seconds[level] = seconds.get(level, 0.0) + (t - last)
        level += delta
        peak = max(peak, level)
        last = t
    return {
        "max_live": peak,
        "seconds_at_level": {str(k): round(v, 3) for k, v in sorted(seconds.items())},
    }


def _item(
    name: str, n: int, minimum: int, measured: dict[str, Any], ok: bool | None
) -> dict[str, Any]:
    if n < minimum:
        return {
            "item": name,
            "verdict": BLOCKED,
            "n": n,
            "reason": f"sample of {n} below the declared minimum {minimum}",
        }
    return {"item": name, "verdict": PASS if ok else FAIL, "n": n, **measured}


def evaluate(data: Data) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    c = data.commit_s
    p50, p95, p99 = (percentile(c, p) for p in (50, 95, 99))
    mx = max(c) if c else None
    items.append(
        _item(
            "NFR-018 commit latency",
            len(c),
            MIN_COMMITS,
            {"p50_s": p50, "p95_s": p95, "p99_s": p99, "max_s": mx},
            c != []
            and p50 is not None
            and p95 is not None
            and mx is not None
            and p50 <= COMMIT_P50_S
            and p95 <= COMMIT_P95_S
            and mx < COMMIT_MAX_S,
        )
    )
    r95 = percentile(data.reply_s, 95)
    items.append(
        _item(
            "NFR-018 full reply",
            len(data.reply_s),
            MIN_REPLIES,
            {"p50_s": percentile(data.reply_s, 50), "p95_s": r95},
            r95 is not None and r95 <= REPLY_P95_S,
        )
    )
    items.append(
        {
            "item": "NFR-018 first visible output",
            "verdict": BLOCKED,
            "reason": "the first streamed token is observed only by the client; no row records "
            "it. Measure it in the browser or the burst client, never from a server proxy.",
        }
    )
    admitted = [a for a in data.attempts if a[1] not in NOT_ADMITTED]
    committed = sum(1 for o, _ in admitted if o == "committed")
    rate = committed / len(admitted) if admitted else None
    items.append(
        _item(
            "NFR-019 commit rate",
            len(admitted),
            MIN_ATTEMPTS,
            {"committed": committed, "admitted": len(admitted), "rate": rate},
            rate is not None and rate >= COMMIT_RATE_MIN,
        )
    )
    sf = sum(1 for r in data.close_reasons if r == "service_failure")
    sf_rate = sf / len(data.close_reasons) if data.close_reasons else None
    items.append(
        _item(
            "NFR-019 service_failure rate",
            len(data.close_reasons),
            MIN_CLOSES,
            {"service_failure": sf, "closes": len(data.close_reasons), "rate": sf_rate},
            sf_rate is not None and sf_rate <= SERVICE_FAILURE_MAX,
        )
    )
    refusals = sum(1 for _, reason in data.attempts if reason == "capacity")
    inputs = {
        "capacity_refusals": refusals,
        "rollbacks_by_reason": _count(
            reason or "none" for o, reason in data.attempts if o != "committed"
        ),
        **concurrency(data.live_intervals),
    }
    verdicts = {i["verdict"] for i in items}
    overall = FAIL if FAIL in verdicts else BLOCKED if BLOCKED in verdicts else PASS
    return {"verdict": overall, "items": items, "nfr_020_inputs": inputs}


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


SQL_COMMIT = (
    "SELECT EXTRACT(EPOCH FROM committed_at - created_at) FROM assess_sessions "
    "WHERE committed_at IS NOT NULL AND created_at >= :since AND created_at < :until"
)
SQL_REPLY = (
    "SELECT EXTRACT(EPOCH FROM b.created_at - s.created_at) FROM assess_turns s "
    "JOIN assess_turns b ON b.session_id = s.session_id AND b.turn_no = s.turn_no + 1 "
    "AND b.role = 'bot' WHERE s.role = 'student' AND s.created_at >= :since "
    "AND s.created_at < :until"
)
SQL_ATTEMPTS = (
    "SELECT outcome, reason_code FROM assess_start_attempts WHERE outcome <> 'requested' "
    "AND created_at >= :since AND created_at < :until"
)
#: the API's advisory refusals write no attempt row, only an ``assess_start_refused`` audit
#: event. ``capacity`` belongs to NFR-020 (and stays out of "admitted"); a worker that is not
#: ready (``worker_not_ready``, ``worker_heartbeat``) is a service-caused refusal and counts
#: against the NFR-019 commit rate. Student- or configuration-caused refusals (rate cap, an
#: activity not configured, a Moodle-side gate) are not Start attempts the service failed.
SQL_REFUSALS = (
    "SELECT 'refused', reason_code FROM assess_audit_events "
    "WHERE event = 'assess_start_refused' "
    "AND reason_code IN ('capacity', 'worker_not_ready', 'worker_heartbeat') "
    "AND created_at >= :since AND created_at < :until"
)
SQL_CLOSES = (
    "SELECT close_reason FROM assess_sessions WHERE state = 'CLOSED' "
    "AND closed_at >= :since AND closed_at < :until"
)
SQL_LIVE = (
    "SELECT EXTRACT(EPOCH FROM committed_at), EXTRACT(EPOCH FROM COALESCE(closed_at, :until)) "
    "FROM assess_sessions WHERE committed_at IS NOT NULL AND committed_at < :until "
    "AND COALESCE(closed_at, :until) >= :since"
)


#: the tables the report reads that the assess revision a0002 puts under row-level security
RLS_TABLES = ("assess_sessions", "assess_start_attempts")
#: the policies that apply to the connecting role's reads of a table. PostgreSQL applies a
#: policy to a role and to every role that has its privileges, i.e. inherits it
#: (``has_privs_of_role``, which ``pg_has_role(..., 'USAGE')`` exposes), and a ``PUBLIC``
#: policy to everyone
_APPLIES = (
    "p.schemaname = 'public' AND p.tablename = c.relname AND p.cmd IN ('SELECT', 'ALL') "
    # CASE, not OR: 'public' is no role, and pg_has_role would raise on it
    "AND EXISTS (SELECT 1 FROM unnest(p.roles) AS r(name) WHERE CASE WHEN r.name = 'public' "
    "THEN true ELSE pg_has_role(current_user, r.name, 'USAGE') END)"
)
#: those of them on which RLS is active for the connecting role (not the owner, no BYPASSRLS,
#: not a superuser: ``row_security_active``) and a read could miss rows: no unconditional
#: PERMISSIVE policy applies to it, or a RESTRICTIVE one applies that is not provably
#: unconditional (its predicate is ANDed with every permissive one)
SQL_HIDDEN = (
    "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
    "AND c.relname = ANY(:t) AND row_security_active(c.oid) "
    "AND (NOT EXISTS (SELECT 1 FROM pg_policies p WHERE " + _APPLIES + " "
    "AND p.permissive = 'PERMISSIVE' AND p.qual = 'true') "
    "OR EXISTS (SELECT 1 FROM pg_policies p WHERE " + _APPLIES + " "
    # a restrictive policy filters reads only through its USING clause: no USING (qual NULL:
    # a FOR ALL / INSERT policy with WITH CHECK only) filters nothing, ``true`` filters nothing
    "AND p.permissive = 'RESTRICTIVE' AND p.qual IS NOT NULL AND p.qual <> 'true')) "
    "ORDER BY 1"
)


class RowSecurityRefusal(Exception):
    """The connecting role would read only the rows RLS lets it see: never a report."""

    def __init__(self, tables: Sequence[str]) -> None:
        self.tables = list(tables)
        super().__init__(
            "row-level security restricts this role on "
            + ", ".join(self.tables)
            + ": it would read an empty or partial campaign. Connect as the superuser, the "
            "tables' owner (the migration user), or a read-only role with BYPASSRLS "
            "(docs/guides/operators/assess-deployment.md, row-level security)."
        )


async def load(cfg: Config) -> Data:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    assert cfg.dsn is not None and cfg.since is not None
    url = cfg.dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    params = {"since": cfg.since, "until": cfg.until or datetime.now(UTC)}
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            hidden = (
                await conn.execute(text(SQL_HIDDEN), {"t": list(RLS_TABLES)})
            ).fetchall()
            if hidden:
                raise RowSecurityRefusal([str(h[0]) for h in hidden])

            async def rows(sql: str) -> list[Any]:
                return list((await conn.execute(text(sql), params)).fetchall())

            return Data(
                commit_s=[float(r[0]) for r in await rows(SQL_COMMIT)],
                reply_s=[float(r[0]) for r in await rows(SQL_REPLY)],
                attempts=[
                    (str(r[0]), r[1])
                    for sql in (SQL_ATTEMPTS, SQL_REFUSALS)
                    for r in await rows(sql)
                ],
                close_reasons=[str(r[0]) for r in await rows(SQL_CLOSES)],
                live_intervals=[
                    (float(r[0]), float(r[1])) for r in await rows(SQL_LIVE)
                ],
            )
    finally:
        await engine.dispose()


def render_markdown(cfg: Config, result: dict[str, Any], stamp: str) -> str:
    out = [
        f"# Assess NFR report: {cfg.label}",
        "",
        f"Generated {stamp} from rows and audit timestamps by `scripts/assess_nfr_report.py`.",
        f"Window: {cfg.since} to {cfg.until or 'now'}.",
        "",
        f"**Verdict: {result['verdict']}**",
        "",
        "| Item | Verdict | n | Measured / reason |",
        "|---|---|---|---|",
    ]
    for i in result.get("items", []):
        detail = i.get("reason") or ", ".join(
            f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in i.items()
            if k not in {"item", "verdict", "n"}
        )
        out.append(f"| {i['item']} | {i['verdict']} | {i.get('n', '-')} | {detail} |")
    for b in result.get("blockers", []):
        out.append(f"| environment | BLOCKED | - | {b} |")
    if "nfr_020_inputs" in result:
        out += ["", "## NFR-020 inputs", "", "```json"]
        out.append(json.dumps(result["nfr_020_inputs"], indent=2))
        out.append("```")
    return "\n".join(out) + "\n"


def write(cfg: Config, result: dict[str, Any], out_dir: Path = RESULTS) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"assess-nfr-{stamp}"
    payload = {
        "label": cfg.label,
        "since": cfg.since.isoformat() if cfg.since else None,
        "until": cfg.until.isoformat() if cfg.until else None,
        "generated_at": stamp,
        **result,
    }
    base.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str))
    base.with_suffix(".md").write_text(render_markdown(cfg, result, stamp))
    return base


def run(cfg: Config, out_dir: Path = RESULTS) -> tuple[int, dict[str, Any]]:
    blockers = cfg.blockers()
    if blockers:
        result: dict[str, Any] = {"verdict": BLOCKED, "items": [], "blockers": blockers}
    else:
        try:
            data = asyncio.run(asyncio.wait_for(load(cfg), QUERY_TIMEOUT_S))
        except RowSecurityRefusal as exc:
            # nothing is judged and nothing is written: a report of the rows one role may see
            # is not the campaign's
            return 3, {"verdict": REFUSED, "items": [], "blockers": [str(exc)]}
        except Exception as exc:  # the database is part of the environment
            result = {
                "verdict": BLOCKED,
                "items": [],
                "blockers": [f"campaign database unreadable: {type(exc).__name__}"],
            }
        else:
            result = evaluate(data)
    write(cfg, result, out_dir)
    return {PASS: 0, FAIL: 1, BLOCKED: 2}[result["verdict"]], result


if __name__ == "__main__":
    code, res = run(Config.from_env())
    print(f"assess NFR report: {res['verdict']}")
    if res["verdict"] == REFUSED:
        print(res["blockers"][0], file=sys.stderr)
    sys.exit(code)
