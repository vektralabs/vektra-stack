#!/usr/bin/env python3
"""NFR-021 transcript timing, measured on a stack run (plan assess-transcript-delivery 10b).

The run itself is an operator procedure: the compose stack with the ``assess`` profile, the
worker's relay pointed at a TLS mail sink that records the Message-ID of every message it
accepts, at least 50 sessions closed concurrently with 2 configured recipients each. This script
then reads what happened and judges it:

- from the database: per session CLOSED since ``--since``, ``closed_at``, the transcript's
  ``generated_at`` and every round-1 delivery row (status, ``sent_at``, Message-ID);
- from the sink: the Message-IDs it holds (one per line, ``--sink-ids``).

Pass criteria (NFR-021, REQ-075): p99(generated - closed) <= 60 s; p95(sent - closed) <= 300 s
over SENT rows; at least 99% of sessions SENT (every recipient) or downloadable within 15 min.

False-pass guards: fewer than ``MIN_SESSIONS`` closed sessions is a FAIL (never percentiles over
a smaller set); a SENT row whose Message-ID the sink does not hold is a FAIL (a status is not
evidence); no SENT row at all is a FAIL. A missing environment is BLOCKED, never a pass.

Usage::

    VEKTRA_NFR_ASSESS_DSN=postgresql://... VEKTRA_NFR_ASSESS_SINCE=2026-11-08T10:00:00Z \\
    VEKTRA_NFR_ASSESS_SINK_IDS=/path/message-ids.txt python scripts/assess_transcript_timing.py
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

MIN_SESSIONS = 50
GENERATION_P99_S = 60.0
SENT_P95_S = 300.0
WITHIN_S = 900.0
WITHIN_RATIO = 0.99


@dataclass(frozen=True)
class Config:
    dsn: str | None
    since: datetime | None
    sink_ids: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        since = e.get("VEKTRA_NFR_ASSESS_SINCE")
        sink = e.get("VEKTRA_NFR_ASSESS_SINK_IDS")
        return cls(
            dsn=e.get("VEKTRA_NFR_ASSESS_DSN") or None,
            since=datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since
            else None,
            sink_ids=Path(sink) if sink else None,
        )


@dataclass(frozen=True)
class Delivery:
    status: str
    sent_at: datetime | None
    message_id: str | None


@dataclass(frozen=True)
class Sample:
    closed_at: datetime
    generated_at: datetime | None
    deliveries: tuple[Delivery, ...] = field(default=())


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; None on an empty set."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def evaluate(
    samples: Sequence[Sample], sink_ids: set[str], min_sessions: int = MIN_SESSIONS
) -> dict[str, Any]:
    problems: list[str] = []
    if len(samples) < min_sessions:
        problems.append(
            f"only {len(samples)} sessions reached CLOSED (at least {min_sessions} needed)"
        )
    gen = [
        (s.generated_at - s.closed_at).total_seconds()
        for s in samples
        if s.generated_at is not None
    ]
    sent = [
        (d.sent_at - s.closed_at).total_seconds()
        for s in samples
        for d in s.deliveries
        if d.status == "SENT" and d.sent_at is not None
    ]
    unconfirmed = [
        d.message_id
        for s in samples
        for d in s.deliveries
        if d.status == "SENT" and (d.message_id or "") not in sink_ids
    ]
    if unconfirmed:
        problems.append(f"{len(unconfirmed)} SENT rows have no message on the sink")
    if not sent:
        problems.append("no SENT row: delivery timing cannot be judged")
    if len(gen) < len(samples):
        problems.append(f"{len(samples) - len(gen)} closed sessions have no transcript")
    p99_gen = percentile(gen, 99)
    p95_sent = percentile(sent, 95)
    if p99_gen is not None and p99_gen > GENERATION_P99_S:
        problems.append(f"p99 generation {p99_gen:.1f} s > {GENERATION_P99_S:g} s")
    if p95_sent is not None and p95_sent > SENT_P95_S:
        problems.append(f"p95 sent {p95_sent:.1f} s > {SENT_P95_S:g} s")

    def ok_in_time(s: Sample) -> bool:
        downloadable = (
            s.generated_at is not None
            and (s.generated_at - s.closed_at).total_seconds() <= WITHIN_S
        )
        all_sent = bool(s.deliveries) and all(
            d.status == "SENT"
            and d.sent_at is not None
            and (d.sent_at - s.closed_at).total_seconds() <= WITHIN_S
            for d in s.deliveries
        )
        return downloadable or all_sent

    ratio = sum(1 for s in samples if ok_in_time(s)) / len(samples) if samples else 0.0
    if ratio < WITHIN_RATIO:
        problems.append(f"{ratio:.1%} SENT or downloadable within 15 min (< 99%)")
    return {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "sessions": len(samples),
        "sent_rows": len(sent),
        "p99_generation_s": p99_gen,
        "p95_sent_s": p95_sent,
        "within_15min_ratio": ratio,
        "at": datetime.now(UTC).isoformat(),
    }


async def blockers(cfg: Config) -> list[str]:
    reasons = []
    if not cfg.dsn:
        reasons.append("VEKTRA_NFR_ASSESS_DSN unset (the stack's Postgres)")
    if cfg.since is None:
        reasons.append("VEKTRA_NFR_ASSESS_SINCE unset (start of the run)")
    if cfg.sink_ids is None or not cfg.sink_ids.is_file():
        reasons.append(
            "VEKTRA_NFR_ASSESS_SINK_IDS unset or missing (the mail sink's evidence)"
        )
    if cfg.dsn:
        try:
            import asyncpg

            conn = await asyncio.wait_for(asyncpg.connect(_pg_dsn(cfg.dsn)), 5)
            await conn.close()
        except Exception as exc:
            reasons.append(f"stack database unreachable ({type(exc).__name__})")
    return reasons


def _pg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


_QUERY = """
SELECT s.id, s.closed_at, t.generated_at,
       COALESCE(json_agg(json_build_object('status', d.status, 'sent_at', d.sent_at,
                                           'message_id', d.message_id))
                FILTER (WHERE d.id IS NOT NULL), '[]') AS deliveries
  FROM assess_sessions s
  LEFT JOIN assess_transcripts t ON t.session_id = s.id
  LEFT JOIN assess_deliveries d ON d.transcript_id = t.id AND d.kind = 'transcript'
       AND d.round = 1 AND d.status <> 'REPLACED'
 WHERE s.state = 'CLOSED' AND s.closed_at >= $1
 GROUP BY s.id, s.closed_at, t.generated_at
"""


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


async def fetch(dsn: str, since: datetime) -> list[Sample]:
    import asyncpg

    conn = await asyncpg.connect(_pg_dsn(dsn))
    try:
        rows = await conn.fetch(_QUERY, since)
    finally:
        await conn.close()
    out = []
    for r in rows:
        ds = r["deliveries"]
        items = json.loads(ds) if isinstance(ds, str) else ds
        out.append(
            Sample(
                closed_at=r["closed_at"],
                generated_at=r["generated_at"],
                deliveries=tuple(
                    Delivery(i["status"], _ts(i["sent_at"]), i["message_id"])
                    for i in items
                ),
            )
        )
    return out


def write_result(doc: Mapping[str, Any], out_dir: Path = RESULTS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"assess-transcript-timing-{stamp}.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


def blocked_result(reasons: Sequence[str]) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "blockers": list(reasons),
        "at": datetime.now(UTC).isoformat(),
    }


async def amain(cfg: Config, out_dir: Path = RESULTS) -> tuple[dict[str, Any], Path]:
    reasons = await blockers(cfg)
    if reasons:
        doc = blocked_result(reasons)
        return doc, write_result(doc, out_dir)
    assert cfg.dsn and cfg.since and cfg.sink_ids
    ids = {ln.strip() for ln in cfg.sink_ids.read_text().splitlines() if ln.strip()}
    doc = evaluate(await fetch(cfg.dsn, cfg.since), ids)
    return doc, write_result(doc, out_dir)


def main() -> int:
    doc, path = asyncio.run(amain(Config.from_env()))
    print(f"{doc['status']}: {path}")
    return {"PASS": 0, "FAIL": 1}.get(doc["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
