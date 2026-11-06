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
#: the fixed reply a human label describes (J.LABEL_REPLY_KEYS)
REPLY = {"fixture": "quiz-math-4.3-0", "question": "q", "reply": "r"}


def _human(n: int = 20) -> dict[str, dict]:
    return {f"h{i}": {**REPLY, **CLEAN, "tone": i % 2 == 0} for i in range(n)}


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
    human = _human()
    verdicts = {k: {c: v[c] for c in J.CRITERIA} for k, v in human.items()}
    for r in _seeded():
        verdicts[r["id"]] = {**CLEAN, r["class"]: True}
    cal = J.calibrate(verdicts, human, _seeded())
    assert cal.passed, cal.reasons
    assert cal.kappa["tone"] == 1.0 and cal.seeded_recall["teacher_intent"] == 1.0


def test_calibration_fails_below_kappa_or_recall() -> None:
    human = _human()
    disagree = {k: {**CLEAN, "tone": not v["tone"]} for k, v in human.items()}
    seeded = _seeded()
    for r in seeded:
        disagree[r["id"]] = {**CLEAN, r["class"]: True}
    cal = J.calibrate(disagree, human, seeded)
    assert cal.status == "FAIL" and any("kappa[tone]" in r for r in cal.reasons)
    missed = {k: {c: v[c] for c in J.CRITERIA} for k, v in human.items()}
    for r in seeded:
        missed[r["id"]] = CLEAN  # the judge catches none of the planted violations
    cal = J.calibrate(missed, human, seeded)
    assert cal.status == "FAIL" and any("seeded_recall" in r for r in cal.reasons)


def test_too_few_seeded_violations_block_the_calibration() -> None:
    human = {"h": {**REPLY, **CLEAN}}
    cal = J.calibrate({"h": CLEAN}, human, _seeded(5))
    assert cal.status == "BLOCKED"


def test_the_committed_seeded_set_meets_the_minimum() -> None:
    rows = J.load_seeded()
    for k in J.JUDGED_HARD:
        assert sum(r["class"] == k for r in rows) >= J.SEEDED_MIN_PER_CLASS


def test_a_label_missing_a_criterion_blocks_instead_of_reading_false() -> None:
    """CodeRabbit PR #151: an omitted criterion is missing evidence, never a ``false``."""
    human = _human()
    del human["h3"]["teacher_intent"]
    verdicts = {k: dict(CLEAN) for k in human}
    for r in _seeded():
        verdicts[r["id"]] = {**CLEAN, r["class"]: True}
    cal = J.calibrate(verdicts, human, _seeded())
    assert cal.status == "BLOCKED" and not cal.kappa
    assert any("h3" in r and "teacher_intent" in r for r in cal.reasons)


def test_a_label_without_its_fixed_reply_blocks() -> None:
    """CodeRabbit PR #151: a label is bound to the reply it describes, never to a probe id."""
    human = _human()
    human["h0"] = {**CLEAN, "tone": True}  # criteria only, no fixture/question/reply
    verdicts = {k: dict(CLEAN) for k in human}
    cal = J.calibrate(verdicts, human, _seeded())
    assert cal.status == "BLOCKED"
    assert any("h0" in r and "reply" in r for r in cal.reasons)


def test_label_loading_is_isolated_and_keeps_rows_as_written(tmp_path: Path) -> None:
    """CodeRabbit PR #151: no reliance on the workspace label file."""
    assert J.load_human_labels(tmp_path / "absent.jsonl") is None
    path = tmp_path / "human_labels.jsonl"
    row = {"id": "h", **REPLY, **CLEAN}
    del row["tone"]
    path.write_text(json.dumps(row) + "\n\n", encoding="utf-8")
    labels = J.load_human_labels(path)
    assert labels == {"h": {k: v for k, v in row.items() if k != "id"}}
    assert "tone" not in labels["h"]  # not coerced to False
    assert J.label_problems(labels) == ["human_label[h] lacks criteria ['tone']"]


def test_bad_label_lines_block_the_calibration_instead_of_vanishing(
    tmp_path: Path,
) -> None:
    """CodeRabbit PR #151 round 2: a bad line, a row without id or a duplicate id is a problem."""
    path = tmp_path / "human_labels.jsonl"
    good = {"id": "h", **REPLY, **CLEAN}
    path.write_text(
        "\n".join(
            [
                json.dumps(good),
                "{not json",
                json.dumps({**REPLY, **CLEAN}),
                json.dumps(good),
            ]
        ),
        encoding="utf-8",
    )
    labels = J.load_human_labels(path)
    assert labels is not None and labels["h"] == {
        k: v for k, v in good.items() if k != "id"
    }
    problems = J.label_problems(labels)
    assert "human_label[line 2] not valid JSON" in problems
    assert "human_label[line 3] no id" in problems
    assert "human_label[line 4] duplicate id 'h'" in problems
    assert J.calibrate({"h": {}}, labels, _seeded()).status == "BLOCKED"
