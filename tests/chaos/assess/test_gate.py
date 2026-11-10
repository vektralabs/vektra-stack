"""The chaos gate refuses every way a chaos run can look green without having proved anything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gate import evaluate

# hermetic, but it belongs to the chaos job, which runs tests/chaos/assess as a whole
pytestmark = pytest.mark.integration

JUNIT_OK = '<testsuite><testcase name="a"/><testcase name="b"/></testsuite>'


def _write(tmp: Path, junit: str, rows: list[dict[str, object]]) -> tuple[Path, Path]:
    j = tmp / "j.xml"
    j.write_text(junit)
    d = tmp / "report"
    d.mkdir()
    (d / "invariants.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return j, d


def _row(name: str, ok: bool = True) -> dict[str, object]:
    return {
        "scenario": name,
        "ok": ok,
        "sessions": 1,
        "expected_sessions": 1,
        "violations": {} if ok else {"key_present": ["x"]},
    }


def test_a_complete_clean_run_passes(tmp_path: Path) -> None:
    j, d = _write(tmp_path, JUNIT_OK, [_row("s1"), _row("negative:x", ok=False)])
    assert evaluate(j, d, 2, 1) == []
    assert "Verdict: PASS" in (d / "invariants.md").read_text()


def test_a_missing_junit_fails(tmp_path: Path) -> None:
    assert evaluate(tmp_path / "none.xml", tmp_path, 1, 0)


def test_a_skipped_case_fails(tmp_path: Path) -> None:
    junit = '<testsuite><testcase name="a"><skipped/></testcase><testcase name="b"/></testsuite>'
    j, d = _write(tmp_path, junit, [_row("s1")])
    assert any("skipped" in p for p in evaluate(j, d, 2, 1))


def test_fewer_cases_or_reports_than_declared_fail(tmp_path: Path) -> None:
    j, d = _write(tmp_path, JUNIT_OK, [_row("s1")])
    problems = evaluate(j, d, 3, 2)
    assert any("cases collected" in p for p in problems)
    assert any("invariant reports" in p for p in problems)


def test_a_violation_in_a_real_scenario_fails(tmp_path: Path) -> None:
    j, d = _write(tmp_path, JUNIT_OK, [_row("close:finished", ok=False)])
    assert any("close:finished" in p for p in evaluate(j, d, 2, 1))
    assert "Verdict: FAIL" in (d / "invariants.md").read_text()
