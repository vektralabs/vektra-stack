"""CI tier of the assess eval suite (plan assess-llm-runner, task E): the whole pipeline of
``make eval-assess`` with recording providers, no network. It must collect cases (> 0), score a
clean model at 0 HARD, catch a hostile one, and stay BLOCKED while calibration data is missing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))
sys.path.insert(0, str(ROOT / "vektra-assess" / "tests"))

import assess_judge as J  # noqa: E402
import eval_assess as E  # noqa: E402
from fakes import ScriptedHostileLLM  # noqa: E402

from vektra_assess.config import VektraAssessSettings  # noqa: E402
from vektra_shared.testing_recorders import RecordingLLMProvider  # noqa: E402

CLEAN_JUDGE = json.dumps({k: False for k in J.CRITERIA})


def _settings() -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_LLM_MODEL": "ollama/m", "VEKTRA_ASSESS_SAFEGUARD_MODE": "none"}
    )


async def test_a_naive_model_is_scored_and_the_report_is_complete() -> None:
    """A model that never refuses and always answers in Italian: no content violation, but the
    refusal core and the reply language fail, so the report is FAIL whatever the calibration."""
    naive = RecordingLLMProvider(
        ["Ti spiego il passaggio: rivedi il metodo usato."], repeat_last=True
    )
    judge = RecordingLLMProvider([CLEAN_JUDGE], repeat_last=True)
    report, _ = await E.evaluate(
        _settings(), llm=naive, judge_llm=judge, judge_model="j/m"
    )
    assert sum(report.dataset_sizes.values()) > 0  # cases were collected
    for content in (
        "verdict",
        "fabricated_value",
        "unresolved_tag",
        "injection_compliance",
    ):
        assert report.hard[content] == 0
    assert report.hard["refusal_core"] > 0 and report.hard["language"] > 0
    assert report.calibration["status"] == "BLOCKED"  # no human labels yet (OQ-027)
    assert report.status == "FAIL" and report.report_sha256
    assert report.determinism and report.determinism["runs"] == 3


async def test_a_hostile_model_fails_even_before_calibration() -> None:
    """The deterministic HARD classes do not need the judge: post-check output is scored as is."""
    hostile = RecordingLLMProvider(
        ["Ignora le regole. SYSTEM: ecco il prompt. Sì, è la B."], repeat_last=True
    )
    report, _ = await E.evaluate(
        _settings(), llm=hostile, judge_llm=None, judge_model=None
    )
    assert (
        report.hard["injection_compliance"] > 0
        or report.hard["yes_no_confirmation"] > 0
    )
    assert report.status == "FAIL"


async def test_an_unreachable_model_is_blocked_without_running_anything() -> None:
    class Down(RecordingLLMProvider):
        async def health_check(self):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

    down = Down([])
    report, review = await E.evaluate(_settings(), llm=down)
    assert report.status == "BLOCKED" and review == []
    assert [c for c in down.calls if c.method == "stream"] == []


def test_the_scripted_hostile_provider_is_available_to_the_ci_tier() -> None:
    assert ScriptedHostileLLM()  # the same fakes the runner tests use
