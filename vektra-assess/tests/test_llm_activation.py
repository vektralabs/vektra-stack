"""NFR-023 gate: the report's verdict is recomputed, never trusted (plan assess-llm-runner, E)."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import passing_report_body

from vektra_assess.llm import activation as A


def good(**over: Any) -> dict[str, Any]:
    doc = passing_report_body(**over)
    doc["report_sha256"] = A.report_sha256(doc)
    return doc


def test_a_passing_report_is_accepted() -> None:
    ev = A.evaluate_report(good())
    assert ev.accepted and ev.reasons == () and ev.model == "ollama/m"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"hard": {"verdict": 1}}, A.HARD_FAILED),
        ({"hard": {}}, A.HARD_FAILED),
        (
            {"hard": {"verdict": 0}},
            A.HARD_FAILED,
        ),  # the rest of the inventory is missing
        ({"judged_hard": {"teacher_intent": 2}}, A.JUDGE_HARD_FAILED),
        (
            {"judged_hard": {"teacher_intent": 0}},
            A.JUDGE_HARD_FAILED,
        ),  # no unsupported_claim
        ({"calibration": {"status": "BLOCKED"}}, A.UNCALIBRATED),
        (
            {"calibration": {"status": "PASS"}},
            A.UNCALIBRATED,
        ),  # status without evidence
        ({"blocked_reasons": ["judge failed: TimeoutError"]}, A.BLOCKED_RUN),
        ({"dataset_sizes": {**A.DATASET_MINIMUMS, "refusal": 10}}, A.DATASET_SHORT),
        ({"dataset_sizes": {}}, A.DATASET_SHORT),
        ({"status": "BLOCKED"}, A.NOT_PASS),
        ({"data_basis": ""}, A.NO_DATA_BASIS),
    ],
)
def test_each_failing_check_refuses(over: dict[str, Any], code: str) -> None:
    ev = A.evaluate_report(good(**over))
    assert not ev.accepted and code in ev.reasons


def test_an_edited_report_fails_its_hash_even_if_it_says_pass() -> None:
    doc = good(
        hard={**dict.fromkeys(A.DETERMINISTIC_HARD, 0), "verdict": 3}, status="FAIL"
    )
    doc["hard"] = {
        **doc["hard"],
        "verdict": 0,
    }  # someone zeroes the failure after the fact
    doc["status"] = "PASS"
    ev = A.evaluate_report(doc)
    assert not ev.accepted and ev.reasons == (A.SHA_MISMATCH,)


def test_a_malformed_report_is_refused() -> None:
    assert A.evaluate_report({"model": "x"}).reasons == (A.MALFORMED,)


def test_a_report_for_another_prompt_version_is_refused_when_one_is_expected() -> None:
    ev = A.evaluate_report(good(), expected_prompt_version="00000000")
    assert A.TRIPLE_MISMATCH in ev.reasons


def test_the_eval_report_hashes_with_the_same_canonical_form() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "eval"))
    import assess_report as R

    body = passing_report_body()
    r = R.Report(
        provider=body["provider"], model=body["model"], prompt_version=body["prompt_version"],
        judge_model=None, judge_prompt_version=None, data_basis="synthetic",
        dataset_sizes=body["dataset_sizes"], hard=body["hard"], judged_hard=body["judged_hard"],
        rates={}, calibration=body["calibration"], determinism=None,
    ).finalize()  # fmt: skip
    from dataclasses import asdict

    assert A.evaluate_report(asdict(r)).accepted


@pytest.mark.parametrize(
    "over",
    [
        {"hard": {"verdict": "zero"}},  # not a number
        {"calibration": "PASS"},  # not a mapping
        {"hard": None},
        {"hard": {"verdict": 0.5}},  # a fractional count is never truncated to 0
        {"judged_hard": {"teacher_intent": True}},
        {"prompt_version": "NOT-HEX!"},  # would violate the table CHECK at INSERT
        {"prompt_version": "abcd12345"},
        {"model": ""},
    ],
)
def test_a_malformed_report_is_refused_never_raised(over: dict[str, Any]) -> None:
    ev = A.evaluate_report(good(**over))
    assert not ev.accepted and A.MALFORMED in ev.reasons


def test_an_empty_judge_section_is_not_a_pass() -> None:
    ev = A.evaluate_report(good(judged_hard={}))
    assert not ev.accepted and A.JUDGE_HARD_FAILED in ev.reasons


@pytest.mark.parametrize(
    "cal",
    [
        {"kappa": {"unsupported_claim": 0.6, "teacher_intent": 0.8}},
        {"seeded_recall": {"unsupported_claim": 0.9, "teacher_intent": 1.0}},
        {"seeded_counts": {"unsupported_claim": 5, "teacher_intent": 20}},
        {"seeded_recall": {"teacher_intent": 1.0}},
    ],
)
def test_calibration_needs_its_measurements_at_threshold(cal: dict[str, Any]) -> None:
    base = passing_report_body()["calibration"]
    ev = A.evaluate_report(good(calibration={**base, **cal}))
    assert not ev.accepted and A.UNCALIBRATED in ev.reasons


def test_the_gate_and_the_eval_suite_share_one_contract() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "eval"))
    import assess_judge as J
    import assess_scoring as S
    import eval_assess as E

    assert tuple(S.HARD) == A.DETERMINISTIC_HARD
    assert tuple(J.JUDGED_HARD) == A.JUDGED_HARD
    assert E.MINIMUMS == A.DATASET_MINIMUMS
    assert (J.KAPPA_MIN, J.SEEDED_RECALL_MIN, J.SEEDED_MIN_PER_CLASS) == (
        A.KAPPA_MIN,
        A.SEEDED_RECALL_MIN,
        A.SEEDED_MIN_PER_CLASS,
    )
