"""Hermetic checks of ``scripts/assess_burst.py`` (plan assess-session-lifecycle M1.11).

Percentiles, breach detection, the report check that rejects a result with a missing outcome, and
the BLOCKED path; nothing here contacts a host. The real burst is ``tests/nfr/test_assess_burst.py``
(operator run); the in-process variant against fakes is
``tests/integration/test_session_burst_inprocess.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_burst.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_burst", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


burst = _load()


def test_percentiles_and_breaches() -> None:
    outcome_cls = burst.Outcome
    ok = [
        outcome_cls(i, i, outcome="commit", start_to_commit_ms=ms)
        for i, ms in enumerate([1000] * 9 + [9000])
    ]
    s = burst.summarize(ok)
    assert s["commit_latency_s"]["p50"] == 1.0 and s["breaches"] == []
    slow = [
        outcome_cls(i, i, outcome="commit", start_to_commit_ms=61_000) for i in range(3)
    ]
    s = burst.summarize(slow)
    assert any("max" in b for b in s["breaches"]) and any(
        "p50" in b for b in s["breaches"]
    )
    none = burst.summarize([outcome_cls(0, 0, outcome="capacity_refused")])
    assert none["breaches"] == ["no Start committed"]
    assert none["outcomes_by_kind"] == {"capacity_refused": 1}


def _doc(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "status": "PASS",
        "model": "hosted_vllm/qwen3.6",
        "n": 2,
        "outcomes": [
            {"index": 0, "outcome": "commit"},
            {"index": 1, "outcome": "capacity_refused"},
        ],
        "row_counts": {"sessions_by_state": {"ACTIVE": 1}},
        "summary": {"breaches": []},
    }
    doc.update(over)
    return doc


def test_the_report_check_rejects_incomplete_results() -> None:
    assert burst.validate_result(_doc()) == []
    assert burst.validate_result(_doc(outcomes=[{"index": 0, "outcome": "commit"}]))
    assert burst.validate_result(
        _doc(
            outcomes=[
                {"index": 0, "outcome": "commit"},
                {"index": 1, "outcome": "pending"},
            ]
        )
    )
    assert burst.validate_result(_doc(model=None))
    assert burst.validate_result(_doc(row_counts={}))
    assert burst.validate_result(
        _doc(summary={"breaches": ["x"]})
    )  # PASS with a breach
    assert burst.validate_result({"status": "BLOCKED", "blockers": ["no env"]}) == []
    assert burst.validate_result({"status": "BLOCKED"})
    assert burst.validate_result({"status": "OK"})


async def test_an_unconfigured_run_is_blocked_and_contacts_nothing(
    tmp_path: Path,
) -> None:
    cfg = burst.Config()  # nothing configured: no host is contacted
    code, path = await burst.amain(cfg, tmp_path)
    assert code == 2
    doc = json.loads(path.read_text())
    assert doc["status"] == "BLOCKED"
    assert any("API" in b for b in doc["blockers"])
    assert any("burst" in b or "manifest" in b for b in doc["blockers"])
    assert burst.validate_result(doc) == []


def test_a_manifest_without_the_burst_fixture_is_a_blocker(tmp_path: Path) -> None:
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"courseid": 10}))
    _b, problem = burst.load_burst(str(m), 40)
    assert problem and "burst" in problem
    m.write_text(json.dumps({"burst": {"cmid": 5, "students": list(range(39))}}))
    assert "39 students" in (burst.load_burst(str(m), 40)[1] or "")
    m.write_text(
        json.dumps(
            {
                "burst": {
                    "cmid": 5,
                    "students": list(range(40)),
                    "files_per_submission": 3,
                }
            }
        )
    )
    assert "files" in (burst.load_burst(str(m), 40)[1] or "")
    m.write_text(
        json.dumps(
            {
                "burst": {
                    "cmid": 5,
                    "students": list(range(40)),
                    "files_per_submission": 10,
                }
            }
        )
    )
    b, problem = burst.load_burst(str(m), 40)
    assert problem is None and b is not None and len(b.students) == 40
