"""LLM judge and its calibration gate (plan assess-llm-runner, task E): CI tier, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import assess_judge as J  # noqa: E402

from vektra_shared.testing_recorders import RecordingLLMProvider  # noqa: E402

CLEAN = {k: False for k in J.CRITERIA}


def test_kappa_values() -> None:
    assert J.cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert J.cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    with pytest.raises(ValueError):
        J.cohen_kappa([], [])


async def test_the_judge_is_pinned_and_parses_a_strict_verdict() -> None:
    llm = RecordingLLMProvider([json.dumps({**CLEAN, "teacher_intent": True})])
    v = await J.judge(llm, "judge/m", "x", "ctx", "q", "r")
    assert v.violations["teacher_intent"] and not v.parse_error
    body = llm.calls[0].request_bytes.decode()
    assert '"temperature":0.0' in body.replace(" ", "") and "judge/m" in body
    assert len(J.JUDGE_PROMPT_VERSION) == 8


@pytest.mark.parametrize(
    "raw", ["non è json", '{"tone": false}', json.dumps({**CLEAN, "tone": "no"})]
)
async def test_an_unreadable_verdict_is_a_violation_never_a_pass(raw: str) -> None:
    v = await J.judge(RecordingLLMProvider([raw]), "m", "x", "c", "q", "r")
    assert v.parse_error and all(v.violations.values())


def _seeded(n: int = 22) -> list[dict]:
    return [{"id": f"s-{k}-{i}", "class": k} for k in J.JUDGED_HARD for i in range(n)]


def test_calibration_is_blocked_without_human_labels() -> None:
    cal = J.calibrate({"a": CLEAN}, None, _seeded())
    assert cal.status == "BLOCKED" and not cal.passed
    assert "OQ-027" in cal.reasons[0]


def test_calibration_passes_on_agreement_and_full_seeded_recall() -> None:
    human = {f"h{i}": {**CLEAN, "tone": i % 2 == 0} for i in range(20)}
    verdicts = dict(human)
    for r in _seeded():
        verdicts[r["id"]] = {**CLEAN, r["class"]: True}
    cal = J.calibrate(verdicts, human, _seeded())
    assert cal.passed, cal.reasons
    assert cal.kappa["tone"] == 1.0 and cal.seeded_recall["teacher_intent"] == 1.0


def test_calibration_fails_below_kappa_or_recall() -> None:
    human = {f"h{i}": {**CLEAN, "tone": i % 2 == 0} for i in range(20)}
    disagree = {k: {**v, "tone": not v["tone"]} for k, v in human.items()}
    seeded = _seeded()
    for r in seeded:
        disagree[r["id"]] = {**CLEAN, r["class"]: True}
    cal = J.calibrate(disagree, human, seeded)
    assert cal.status == "FAIL" and any("kappa[tone]" in r for r in cal.reasons)
    missed = dict(human)
    for r in seeded:
        missed[r["id"]] = CLEAN  # the judge catches none of the planted violations
    cal = J.calibrate(missed, human, seeded)
    assert cal.status == "FAIL" and any("seeded_recall" in r for r in cal.reasons)


def test_too_few_seeded_violations_block_the_calibration() -> None:
    human = {"h": CLEAN}
    cal = J.calibrate({"h": CLEAN}, human, _seeded(5))
    assert cal.status == "BLOCKED"


def test_the_committed_seeded_set_meets_the_minimum() -> None:
    rows = J.load_seeded()
    for k in J.JUDGED_HARD:
        assert sum(r["class"] == k for r in rows) >= J.SEEDED_MIN_PER_CLASS
    assert (
        J.load_human_labels() is None
    )  # human labels are the owner's (OQ-027): none yet
