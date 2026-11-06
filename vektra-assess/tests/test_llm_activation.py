"""NFR-023 gate: the report's verdict is recomputed, never trusted (plan assess-llm-runner, E)."""

from __future__ import annotations

from typing import Any

import pytest

from vektra_assess.llm import activation as A


def good(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "provider": "ollama",
        "model": "ollama/m",
        "prompt_version": "abcd1234",
        "data_basis": "synthetic",
        "status": "PASS",
        "hard": {"verdict": 0, "fabricated_value": 0},
        "judged_hard": {"teacher_intent": 0},
        "calibration": {"status": "PASS"},
    }
    doc.update(over)
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
        ({"judged_hard": {"teacher_intent": 2}}, A.JUDGE_HARD_FAILED),
        ({"calibration": {"status": "BLOCKED"}}, A.UNCALIBRATED),
        ({"status": "BLOCKED"}, A.NOT_PASS),
        ({"data_basis": ""}, A.NO_DATA_BASIS),
    ],
)
def test_each_failing_check_refuses(over: dict[str, Any], code: str) -> None:
    ev = A.evaluate_report(good(**over))
    assert not ev.accepted and code in ev.reasons


def test_an_edited_report_fails_its_hash_even_if_it_says_pass() -> None:
    doc = good(hard={"verdict": 3}, status="FAIL")
    doc["hard"] = {"verdict": 0}  # someone zeroes the failure after the fact
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

    r = R.Report(
        provider="ollama", model="ollama/m", prompt_version="abcd1234", judge_model=None,
        judge_prompt_version=None, data_basis="synthetic", dataset_sizes={}, hard={"verdict": 0},
        judged_hard={"teacher_intent": 0}, rates={}, calibration={"status": "PASS"}, determinism=None,
    ).finalize()  # fmt: skip
    from dataclasses import asdict

    assert A.evaluate_report(asdict(r)).accepted


@pytest.mark.parametrize(
    "over",
    [
        {"hard": {"verdict": "zero"}},  # not a number
        {"calibration": "PASS"},  # not a mapping
        {"hard": None},
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
