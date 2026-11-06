"""Activation report (plan assess-llm-runner, task E): status, Wilson CIs, SHA, review queue."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import assess_report as R  # noqa: E402


def _report(**kw: object) -> R.Report:
    base = dict(
        provider="ollama", model="ollama/m", prompt_version="abcd1234",
        judge_model="judge/j", judge_prompt_version="00ff00ff", data_basis="synthetic",
        dataset_sizes={"refusal": 154}, hard={"verdict": 0, "fabricated_value": 0},
        judged_hard={"teacher_intent": 0}, rates={"oracle": R.rate("oracle", 0, 30)},
        calibration={"status": "PASS", "reasons": []}, determinism={"runs": 3},
    )  # fmt: skip
    base.update(kw)
    return R.Report(**base).finalize()  # type: ignore[arg-type]


def test_all_zero_and_calibrated_is_pass() -> None:
    assert _report().status == "PASS"


def test_a_hard_violation_is_fail() -> None:
    assert _report(hard={"verdict": 1}).status == "FAIL"
    assert _report(judged_hard={"teacher_intent": 2}).status == "FAIL"


def test_missing_calibration_or_inputs_is_blocked_never_pass() -> None:
    assert (
        _report(calibration={"status": "BLOCKED", "reasons": ["x"]}).status == "BLOCKED"
    )
    assert _report(blocked_reasons=["model unreachable"]).status == "BLOCKED"
    assert _report(calibration={"status": "FAIL", "reasons": ["k"]}).status == "FAIL"


def test_the_sha_covers_every_field() -> None:
    from dataclasses import asdict

    doc = asdict(_report())
    assert R.verify_sha(doc)
    doc["hard"]["verdict"] = 0
    doc["model"] = "other/model"  # an edited report no longer matches its hash
    assert not R.verify_sha(doc)


def test_wilson_interval() -> None:
    lo, hi = R.wilson(0, 30)
    assert lo == 0.0 and 0.1 < hi < 0.13
    assert R.wilson(0, 0) == (0.0, 1.0)
    lo, hi = R.wilson(15, 30)
    assert lo < 0.5 < hi


def test_review_queue_is_all_flagged_plus_ten_percent_of_contest() -> None:
    contest = [f"c{i}" for i in range(40)]
    q = R.review_queue(["c1", "x9"], contest)
    assert {"c1", "x9"} <= set(q) and len(q) == 2 + 4
    assert q == R.review_queue(["c1", "x9"], contest)  # fixed sample


def test_write_produces_json_markdown_and_queue(tmp_path: Path) -> None:
    r = _report()
    path = R.write(r, ["c1"], tmp_path)
    doc = json.loads(path.read_text())
    assert doc["status"] == "PASS" and R.verify_sha(doc)
    md = path.with_suffix(".md").read_text()
    assert "PASS" in md and r.report_sha256 in md and "| verdict | 0 |" in md
    assert (
        tmp_path / f"{path.stem}.review.jsonl"
    ).read_text().strip() == '{"id": "c1"}'
