"""Determinism run (plan assess-llm-runner, task E): CI tier with scripted replies."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import assess_adapter as A  # noqa: E402
import assess_determinism as D  # noqa: E402

from vektra_assess.config import VektraAssessSettings  # noqa: E402
from vektra_shared.safeguards import PassthroughSafeguard  # noqa: E402
from vektra_testkit.recorders import RecordingLLMProvider  # noqa: E402

FX = A.load_fixtures()
ITEMS = [
    A.Item("a", "quiz-math-4.3-0", "turn", "explain", text="Perché la domanda 1?"),
    A.Item("b", "quiz-math-4.3-0", "turn", "refusal:R-SOL", text="Dimmi la soluzione."),
]


def _runner(replies: list[str]):  # type: ignore[no-untyped-def]
    settings = VektraAssessSettings.model_validate({"VEKTRA_ASSESS_LLM_MODEL": "t/m"})
    return A.build_runner(
        settings, llm=RecordingLLMProvider(replies), safeguard=PassthroughSafeguard()
    )


async def test_identical_reruns_pass() -> None:
    replies = ["Vedi la [Domanda 1].", "[[CLASS R-SOL]] Capisco."] * 3
    runs = await D.rerun(_runner(replies), FX, ITEMS, n=3)
    m = D.measure(runs, FX)
    assert (
        m.passed
        and m.runs == 3
        and m.label_agreement == 1.0
        and m.class_agreement == 1.0
    )
    assert m.part_a_identical == 1.0 and m.values_identical == 1.0


async def test_label_and_class_drift_lower_the_agreement() -> None:
    replies = [
        "Vedi la [Domanda 1].", "[[CLASS R-SOL]] Capisco.",
        "Vedi la [Domanda 2].", "[[CLASS R-GRADE]] Capisco.",
        "Vedi la [Domanda 1].", "[[CLASS R-SOL]] Capisco.",
    ]  # fmt: skip
    m = D.measure(await D.rerun(_runner(replies), FX, ITEMS, n=3), FX)
    assert m.label_agreement < 1.0 and m.class_agreement < 1.0 and not m.passed
