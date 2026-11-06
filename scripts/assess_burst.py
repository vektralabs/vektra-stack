#!/usr/bin/env python3
"""Early Start burst (plan assess-session-lifecycle M1.11, NFR-018 / NFR-020).

N (default 40) students start a discussion at the same moment, each on an assignment with a
10-file submission seeded on the dev Moodle. For every Start the run records:

- the client-side mint -> 202 latency;
- the outcome: ``commit`` / ``capacity_refused`` (503 at Start, nothing consumed) /
  ``rolled_back:<reason>`` / ``refused:<code>`` / ``timeout`` / ``error:<type>``;
- the Start -> commit latency from the REQ-085 audit timestamps (``assess_start`` and
  ``assess_commit`` rows, database clock), never from a status field;

then the worker RSS (``docker stats``, when a container is named) and the row counts of sessions
by state, snapshots, session keys and hand-off rows for the burst's students. The result goes to
``tests/nfr/results/assess-burst-<utc>.json``.

**Nothing is contacted unless configured.** Preconditions are checked first; when any is missing
or unreachable the run writes a ``BLOCKED`` result naming every blocker and exits 2 (pytest reports
SKIPPED). The target LLM is whatever the API under test is configured with; this script only reads
its ``/models`` endpoint as a reachability check when ``--llm-base`` is given. It is an operator
tool: the user runs it against the dev stack, the dev Moodle and the candidate model (OQ-026).

Configuration (flags or environment):

    --api / VEKTRA_ASSESS_BURST_API          base URL of the API under test (http://localhost:8000)
    --mint-key / VEKTRA_ASSESS_BURST_MINT_KEY   the dev site's assess:mint API key
    --manifest / VEKTRA_ASSESS_BURST_MANIFEST   seed manifest with a "burst" section:
                                             {"burst": {"cmid": <id>, "courseid": <id>,
                                              "students": [<userid>, ...], "files_per_submission": 10}}
    --dsn / VEKTRA_ASSESS_BURST_DSN          Postgres DSN of the dev stack (audit timestamps, counts)
    --moodle / VEKTRA_ASSESS_BURST_MOODLE    dev Moodle base URL (reachability)
    --llm-base / VEKTRA_ASSESS_BURST_LLM_BASE   optional: the model endpoint, GET <base>/models
    --model / VEKTRA_ASSESS_BURST_MODEL      the model label recorded in the result (required)
    --worker-container / VEKTRA_ASSESS_BURST_WORKER   optional: container name for the RSS sample
    -n / VEKTRA_ASSESS_BURST_N               number of concurrent Starts (default 40)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "tests" / "nfr" / "results"
#: NFR-018 commit latency targets (seconds), NFR-020 is the refusal-without-consumption check
P50_S, P95_S, MAX_S = 10.0, 30.0, 60.0
STATUSES = ("PASS", "BREACH", "BLOCKED")


@dataclass
class Config:
    api: str | None = None
    mint_key: str | None = None
    manifest: str | None = None
    dsn: str | None = None
    moodle: str | None = None
    llm_base: str | None = None
    model: str | None = None
    worker_container: str | None = None
    n: int = 40
    timeout_s: float = 120.0
    poll_s: float = 0.5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        e = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = e.get(f"VEKTRA_ASSESS_BURST_{name}", "").strip()
            return value or None

        return cls(
            api=get("API"),
            mint_key=get("MINT_KEY"),
            manifest=get("MANIFEST"),
            dsn=get("DSN"),
            moodle=get("MOODLE"),
            llm_base=get("LLM_BASE"),
            model=get("MODEL"),
            worker_container=get("WORKER"),
            n=int(get("N") or 40),
        )


@dataclass
class Outcome:
    index: int
    sub: int
    session_id: str | None = None
    outcome: str = "pending"
    mint_to_202_ms: int | None = None
    start_to_commit_ms: int | None = None


@dataclass
class Burst:
    cmid: int
    courseid: int | None
    students: list[int]
    files_per_submission: int | None


# --- preconditions ---------------------------------------------------------------------------


def load_burst(manifest: str | None, n: int) -> tuple[Burst | None, str | None]:
    if not manifest:
        return None, "manifest not configured (VEKTRA_ASSESS_BURST_MANIFEST)"
    try:
        doc = json.loads(Path(manifest).read_text())
    except (OSError, json.JSONDecodeError):
        return None, "manifest unreadable"
    b = doc.get("burst") if isinstance(doc, dict) else None
    if not isinstance(b, dict) or not isinstance(b.get("students"), list):
        return None, (
            "manifest has no 'burst' section: the 40 x 10-file assignment fixture is not seeded "
            "(vektra-moodle seeder request, plan Notes)"
        )
    try:
        cmid = int(b["cmid"])
        students = [int(s) for s in b["students"]]
        files = b.get("files_per_submission")
        files = int(files) if files is not None else None
        courseid = b.get("courseid")
        courseid = int(courseid) if courseid is not None else None
    except (KeyError, TypeError, ValueError):
        # a malformed section is a blocker like a missing one, never a crash
        return (
            None,
            "manifest 'burst' section is malformed (cmid, students, files, courseid)",
        )
    if len(students) < n:
        return None, f"burst fixture has {len(students)} students, {n} needed"
    if files is not None and files < 10:
        return None, f"burst fixture has {files} files per submission, 10 needed"
    return Burst(cmid, courseid, students[:n], files), None


async def _reachable(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.get(url, timeout=5)
    except httpx.HTTPError:
        return False
    return r.status_code < 500


async def blockers(cfg: Config) -> list[str]:
    """Every reason the burst cannot run here; empty when it can. Contacts only what is set."""
    out: list[str] = []
    for name, value in (
        ("API (VEKTRA_ASSESS_BURST_API)", cfg.api),
        ("mint key (VEKTRA_ASSESS_BURST_MINT_KEY)", cfg.mint_key),
        ("DSN (VEKTRA_ASSESS_BURST_DSN)", cfg.dsn),
        ("dev Moodle (VEKTRA_ASSESS_BURST_MOODLE)", cfg.moodle),
        ("model label (VEKTRA_ASSESS_BURST_MODEL)", cfg.model),
    ):
        if not value:
            out.append(f"{name} not configured")
    _burst, problem = load_burst(cfg.manifest, cfg.n)
    if problem:
        out.append(problem)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        if cfg.api:
            try:
                r = await client.get(f"{cfg.api.rstrip('/')}/health/assess", timeout=5)
                healthy = r.status_code == 200 and r.json().get("status") == "healthy"
            except (httpx.HTTPError, ValueError):
                healthy = False
            if not healthy:
                out.append(
                    "API /health/assess is not healthy (assess disabled or worker down)"
                )
        if cfg.moodle and not await _reachable(client, cfg.moodle):
            out.append("dev Moodle unreachable")
        if cfg.llm_base and not await _reachable(
            client, f"{cfg.llm_base.rstrip('/')}/models"
        ):
            out.append("target LLM endpoint unreachable")
    if cfg.dsn:
        try:
            import asyncpg

            conn = await asyncio.wait_for(asyncpg.connect(_pg_dsn(cfg.dsn)), 5)
            await conn.close()
        except Exception:
            out.append("database unreachable with the configured DSN")
    return out


def _pg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


# --- the burst -------------------------------------------------------------------------------


async def one_start(
    client: httpx.AsyncClient,
    o: Outcome,
    *,
    cmid: int,
    courseid: int | None,
    mint_key: str,
    gate: asyncio.Event,
    timeout_s: float,
    poll_s: float,
) -> None:
    await gate.wait()  # every Start leaves at the same moment
    t0 = time.monotonic()
    try:
        r = await client.post(
            "/api/v1/assess/tokens",
            json={
                "sub": o.sub,
                "cmid": cmid,
                "courseid": courseid,
                "loginas": False,
                "realuser_id": None,
                "plugin_version": "99.0.0",
            },
            headers={"Authorization": f"Bearer {mint_key}"},
        )
        if r.status_code != 201:
            o.outcome = f"refused:mint_{r.status_code}"
            return
        token = r.json()["token"]
        r = await client.post(
            "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        o.mint_to_202_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 503 and _reason(r) == "capacity":
            o.outcome = "capacity_refused"
            return
        if r.status_code != 202:
            o.outcome = f"refused:{_code(r)}:{_reason(r) or r.status_code}"
            return
        o.session_id = r.json()["session_id"]
        session_token = r.json()["token"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            g = await client.get(
                f"/api/v1/assess/sessions/{o.session_id}",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            if g.status_code == 200 and g.json()["state"] == "ACTIVE":
                o.outcome = "commit"
                return
            if g.status_code == 404:
                o.outcome = (
                    "rolled_back"  # the reason is read from the database afterwards
                )
                return
            await asyncio.sleep(poll_s)
        o.outcome = "timeout"
    except Exception as exc:  # recorded, never raised: every Start gets an outcome
        o.outcome = f"error:{type(exc).__name__}"


def _body(r: httpx.Response) -> dict[str, Any]:
    try:
        doc = r.json()
    except ValueError:
        return {}
    return (doc.get("detail") or doc).get("error", {}) if isinstance(doc, dict) else {}


def _code(r: httpx.Response) -> str:
    return str(_body(r).get("code", r.status_code))


def _reason(r: httpx.Response) -> str | None:
    details = _body(r).get("details") or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    return str(reason) if reason else None


def percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[k]


def summarize(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    commits = [
        o.start_to_commit_ms / 1000
        for o in outcomes
        if o.start_to_commit_ms is not None
    ]
    by: dict[str, int] = {}
    for o in outcomes:
        key = (
            o.outcome.split(":")[0]
            if o.outcome.startswith(("refused", "error"))
            else o.outcome
        )
        by[key] = by.get(key, 0) + 1
    p50, p95, mx = (
        percentile(commits, 50),
        percentile(commits, 95),
        (max(commits) if commits else None),
    )
    breaches = []
    if p50 is not None and p50 > P50_S:
        breaches.append(f"commit p50 {p50:.1f}s > {P50_S:.0f}s")
    if p95 is not None and p95 > P95_S:
        breaches.append(f"commit p95 {p95:.1f}s > {P95_S:.0f}s")
    if mx is not None and mx >= MAX_S:
        breaches.append(f"commit max {mx:.1f}s >= {MAX_S:.0f}s")
    if not commits:
        breaches.append("no Start committed")
    return {
        "outcomes_by_kind": by,
        "commit_latency_s": {"p50": p50, "p95": p95, "max": mx, "n": len(commits)},
        "breaches": breaches,
    }


CountsFn = Callable[[Sequence[Outcome]], Awaitable[dict[str, Any]]]


async def run_burst(
    client: httpx.AsyncClient,
    *,
    burst: Burst,
    mint_key: str,
    model: str,
    counts: CountsFn,
    timeout_s: float = 120.0,
    poll_s: float = 0.5,
    rss: Callable[[], str | None] = lambda: None,
) -> dict[str, Any]:
    outcomes = [Outcome(i, sub) for i, sub in enumerate(burst.students)]
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(
            one_start(
                client,
                o,
                cmid=burst.cmid,
                courseid=burst.courseid,
                mint_key=mint_key,
                gate=gate,
                timeout_s=timeout_s,
                poll_s=poll_s,
            )
        )
        for o in outcomes
    ]
    started_at = datetime.now(UTC).isoformat()
    gate.set()
    await asyncio.gather(*tasks)
    rows = await counts(outcomes)
    summary = summarize(outcomes)
    return {
        "status": "BREACH" if summary["breaches"] else "PASS",
        "model": model,
        "started_at": started_at,
        "n": len(outcomes),
        "cmid": burst.cmid,
        "files_per_submission": burst.files_per_submission,
        "outcomes": [asdict(o) for o in outcomes],
        "summary": summary,
        "row_counts": rows,
        "worker_rss": rss(),
    }


async def db_counts(dsn: str) -> CountsFn:
    """Audit-timestamp latencies, rollback reasons and row counts, read with the given DSN."""
    import asyncpg

    async def counts(outcomes: Sequence[Outcome]) -> dict[str, Any]:
        sids = [o.session_id for o in outcomes if o.session_id]
        conn = await asyncpg.connect(_pg_dsn(dsn))
        try:
            lat = await conn.fetch(
                "SELECT s.session_id::text AS sid, extract(epoch FROM c.created_at - "
                "s.created_at) * 1000 AS ms FROM assess_audit_events s JOIN "
                "assess_audit_events c ON c.session_id = s.session_id AND c.event = "
                "'assess_commit' WHERE s.event = 'assess_start' AND s.session_id::text = "
                "ANY($1::text[])",
                sids,
            )
            reasons = await conn.fetch(
                "SELECT session_id::text AS sid, reason_code FROM assess_start_attempts "
                "WHERE outcome = 'rolled_back' AND session_id::text = ANY($1::text[])",
                sids,
            )
            by_sid = {r["sid"]: int(r["ms"]) for r in lat}
            why = {r["sid"]: r["reason_code"] for r in reasons}
            for o in outcomes:
                if o.session_id in by_sid:
                    o.start_to_commit_ms = by_sid[o.session_id]
                if o.outcome == "rolled_back" and o.session_id in why:
                    o.outcome = f"rolled_back:{why[o.session_id]}"
            out: dict[str, Any] = {
                "sessions_by_state": {
                    r["state"]: r["n"]
                    for r in await conn.fetch(
                        "SELECT state, count(*) AS n FROM assess_sessions WHERE id::text = "
                        "ANY($1::text[]) GROUP BY state",
                        sids,
                    )
                }
            }
            for table in (
                "assess_snapshots",
                "assess_session_keys",
                "assess_session_handoff",
            ):
                out[table] = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE session_id::text = ANY($1::text[])",
                    sids,
                )
            return out
        finally:
            await conn.close()

    return counts


def worker_rss(container: str | None) -> Callable[[], str | None]:
    def sample() -> str | None:
        if not container:
            return None
        res = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip() or None

    return sample


# --- result file -----------------------------------------------------------------------------


def validate_result(doc: Mapping[str, Any]) -> list[str]:
    """The report check (false-pass guard): a result missing any of the N outcomes, its model or
    its row counts is rejected, whatever its status says."""
    problems: list[str] = []
    status = doc.get("status")
    if status not in STATUSES:
        return [f"unknown status {status!r}"]
    if status == "BLOCKED":
        if not doc.get("blockers"):
            problems.append("BLOCKED without a reason")
        return problems
    if not doc.get("model"):
        problems.append("no model recorded")
    n = int(doc.get("n") or 0)
    outcomes = doc.get("outcomes") or []
    if n <= 0 or len(outcomes) != n:
        problems.append(f"{len(outcomes)} outcomes for n={n}")
    missing = [
        o.get("index") for o in outcomes if o.get("outcome") in (None, "", "pending")
    ]
    if missing:
        problems.append(f"outcomes missing for Starts {missing}")
    if (
        not isinstance(doc.get("row_counts"), dict)
        or "sessions_by_state" not in doc["row_counts"]
    ):
        problems.append("no row counts")
    if status == "PASS" and (doc.get("summary") or {}).get("breaches"):
        problems.append("PASS with breaches")
    return problems


def write_result(doc: Mapping[str, Any], out_dir: Path = RESULTS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"assess-burst-{stamp}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


def blocked_result(reasons: Sequence[str], cfg: Config) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "blockers": list(reasons),
        "model": cfg.model,
        "n": cfg.n,
        "at": datetime.now(UTC).isoformat(),
    }


async def amain(cfg: Config, out_dir: Path = RESULTS) -> tuple[int, Path]:
    reasons = await blockers(cfg)
    if reasons:
        return 2, write_result(blocked_result(reasons, cfg), out_dir)
    burst, _ = load_burst(cfg.manifest, cfg.n)
    assert burst is not None and cfg.api and cfg.mint_key and cfg.dsn and cfg.model
    limits = httpx.Limits(max_connections=cfg.n * 2)
    async with httpx.AsyncClient(
        base_url=cfg.api, timeout=cfg.timeout_s, limits=limits, follow_redirects=False
    ) as client:
        doc = await run_burst(
            client,
            burst=burst,
            mint_key=cfg.mint_key,
            model=cfg.model,
            counts=await db_counts(cfg.dsn),
            timeout_s=cfg.timeout_s,
            poll_s=cfg.poll_s,
            rss=worker_rss(cfg.worker_container),
        )
    problems = validate_result(doc)
    if problems:
        doc["status"] = "BREACH"
        doc.setdefault("summary", {}).setdefault("breaches", []).extend(problems)
    return (0 if doc["status"] == "PASS" else 1), write_result(doc, out_dir)


def parse_args(argv: Sequence[str] | None = None) -> Config:
    cfg = Config.from_env()
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    for flag, attr in (
        ("--api", "api"),
        ("--mint-key", "mint_key"),
        ("--manifest", "manifest"),
        ("--dsn", "dsn"),
        ("--moodle", "moodle"),
        ("--llm-base", "llm_base"),
        ("--model", "model"),
        ("--worker-container", "worker_container"),
    ):
        p.add_argument(flag, dest=attr, default=getattr(cfg, attr))
    p.add_argument("-n", type=int, default=cfg.n)
    p.add_argument("--timeout", dest="timeout_s", type=float, default=cfg.timeout_s)
    args = p.parse_args(argv)
    return Config(**{k: v for k, v in vars(args).items()})


def main(argv: Sequence[str] | None = None) -> int:
    cfg = parse_args(argv)
    code, path = asyncio.run(amain(cfg))
    doc = json.loads(path.read_text())
    print(f"{doc['status']}: {path}")
    for reason in doc.get("blockers", []):
        print(f"  blocked: {reason}")
    for breach in (doc.get("summary") or {}).get("breaches", []):
        print(f"  breach: {breach}")
    return code


if __name__ == "__main__":
    sys.exit(main())
