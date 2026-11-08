"""Hermetic checks of ``scripts/assess_transcript_timing.py`` (NFR-021, plan 10b).

The judgement over a run: percentiles, the 15-minute rule, and the false-pass guards (fewer than
50 sessions, a SENT row the sink does not hold, no SENT row) plus the BLOCKED path. Nothing here
contacts a host; the real measurement is ``tests/nfr/test_assess_transcript_timing.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_transcript_timing.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_transcript_timing", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


t = _load()
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def _sample(gen_s: float = 5, sent_s: float = 30, n_rcpt: int = 2, i: int = 0):  # type: ignore[no-untyped-def]
    return t.Sample(
        closed_at=T0,
        generated_at=T0 + timedelta(seconds=gen_s),
        deliveries=tuple(
            t.Delivery("SENT", T0 + timedelta(seconds=sent_s), f"<{i}-{k}@x>")
            for k in range(n_rcpt)
        ),
    )


def _ids(samples) -> set[str]:  # type: ignore[no-untyped-def]
    return {d.message_id for s in samples for d in s.deliveries}


def test_a_healthy_run_passes() -> None:
    samples = [_sample(i=i) for i in range(50)]
    doc = t.evaluate(samples, _ids(samples))
    assert doc["status"] == "PASS", doc["problems"]
    assert doc["sessions"] == 50 and doc["sent_rows"] == 100
    assert doc["p99_generation_s"] == 5 and doc["p95_sent_s"] == 30


def test_fewer_than_fifty_sessions_fail_rather_than_report() -> None:
    samples = [_sample(i=i) for i in range(49)]
    doc = t.evaluate(samples, _ids(samples))
    assert doc["status"] == "FAIL" and "only 49 sessions" in doc["problems"][0]


def test_a_sent_row_without_a_sink_message_fails() -> None:
    samples = [_sample(i=i) for i in range(50)]
    ids = _ids(samples) - {"<0-0@x>"}
    doc = t.evaluate(samples, ids)
    assert doc["status"] == "FAIL"
    assert any("no message on the sink" in p for p in doc["problems"])


def test_the_thresholds() -> None:
    slow_gen = [_sample(gen_s=61 if i == 49 else 5, i=i) for i in range(50)]
    assert any(
        "p99 generation" in p for p in t.evaluate(slow_gen, _ids(slow_gen))["problems"]
    )
    slow_sent = [_sample(sent_s=301 if i >= 45 else 30, i=i) for i in range(50)]
    assert any(
        "p95 sent" in p for p in t.evaluate(slow_sent, _ids(slow_sent))["problems"]
    )
    none_sent = [
        t.Sample(T0, T0 + timedelta(seconds=5), (t.Delivery("RETRYING", None, None),))
        for _ in range(50)
    ]
    doc = t.evaluate(none_sent, set())
    assert doc["status"] == "FAIL" and any("no SENT row" in p for p in doc["problems"])
    # downloadable within 15 min counts even when the mail is late (REQ-075)
    assert doc["within_15min_ratio"] == 1.0
    late = [t.Sample(T0, T0 + timedelta(minutes=20), ()) for _ in range(50)]
    assert any("within 15 min" in p for p in t.evaluate(late, set())["problems"])


def test_percentile_is_nearest_rank() -> None:
    assert t.percentile([], 95) is None
    assert t.percentile(list(range(1, 101)), 95) == 95
    assert t.percentile([3.0], 99) == 3.0


async def test_a_missing_environment_is_blocked(tmp_path: Path) -> None:
    doc, path = await t.amain(t.Config.from_env({}), out_dir=tmp_path)
    assert doc["status"] == "BLOCKED" and len(doc["blockers"]) == 3
    assert json.loads(path.read_text())["status"] == "BLOCKED"


def test_unfinished_deliveries_count_against_p95() -> None:
    """CodeRabbit 4504362573: a delivery not SENT is a miss, not a sample left out."""
    samples = [_sample(i=i) for i in range(50)]
    for s in samples[:6]:  # 6 of 100 deliveries never sent: more than 5%, p95 fails
        object.__setattr__(
            s,
            "deliveries",
            (s.deliveries[0], t.Delivery("RETRYING", None, None)),
        )
    doc = t.evaluate(samples, _ids(samples))
    assert doc["status"] == "FAIL" and any("p95 sent" in p for p in doc["problems"])


def test_the_workload_must_be_the_benchmark_one() -> None:
    """CodeRabbit 4504362566: 2 recipients per session, closed concurrently."""
    one = [_sample(n_rcpt=1, i=i) for i in range(50)]
    assert any("2 recipients" in p for p in t.evaluate(one, _ids(one))["problems"])
    spread = [
        t.Sample(
            T0 + timedelta(minutes=i),
            T0 + timedelta(minutes=i, seconds=5),
            tuple(
                t.Delivery(
                    "SENT", T0 + timedelta(minutes=i, seconds=30), f"<{i}-{k}@x>"
                )
                for k in range(2)
            ),
        )
        for i in range(50)
    ]
    assert any("concurrent" in p for p in t.evaluate(spread, _ids(spread))["problems"])
