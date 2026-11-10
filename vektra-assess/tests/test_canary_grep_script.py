"""The REQ-080 canary grep script and its false-pass guards (plan assess-privacy-retention 12b).

The script itself is the evidence collector of the compose run; here every way it could pass
without proving anything is shown to fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "nfr" / "assess_canary_grep.sh"
CANARY = "CNRY-right_answer-multichoice-1a2b3c4d"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(tmp: Path, *, seed: str, sinks: dict[str, str], canaries: str = CANARY) -> int:
    (tmp / "canaries.txt").write_text(f"# planted\n{canaries}\n")
    (tmp / "seed.txt").write_text(seed)
    paths = []
    for name, body in sinks.items():
        p = tmp / name
        if body is not None:
            p.write_text(body)
        paths.append(str(p))
    res = subprocess.run(
        ["bash", str(SCRIPT), "--canaries", str(tmp / "canaries.txt"),
         "--seed", str(tmp / "seed.txt"), *paths],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert CANARY not in res.stdout + res.stderr  # the report names, never echoes
    return res.returncode


def test_clean_sinks_pass(tmp_path: Path) -> None:
    sinks = {
        "api.log": '{"event": "ok"}\n',
        "metrics.txt": "vektra_assess_metrics_up 1\n",
    }
    assert _run(tmp_path, seed=f"answer: {CANARY}", sinks=sinks) == 0


def test_a_canary_in_any_sink_fails(tmp_path: Path) -> None:
    sinks = {"api.log": "fine\n", "worker.log": f"leaked {CANARY}\n"}
    assert _run(tmp_path, seed=f"answer: {CANARY}", sinks=sinks) == 1


@pytest.mark.parametrize(
    "case",
    ["canary_not_planted", "missing_sink", "empty_sink", "no_canary"],
)
def test_every_false_pass_is_blocked(tmp_path: Path, case: str) -> None:
    seed = f"answer: {CANARY}"
    sinks: dict[str, str] = {"api.log": "fine\n"}
    canaries = CANARY
    if case == "canary_not_planted":
        seed = "nothing planted here"
    elif case == "missing_sink":
        sinks["parser.log"] = None  # type: ignore[assignment]
    elif case == "empty_sink":
        sinks["mail.log"] = ""
    elif case == "no_canary":
        canaries = ""
    assert _run(tmp_path, seed=seed, sinks=sinks, canaries=canaries) == 2


def test_an_unknown_option_is_refused(tmp_path: Path) -> None:
    res = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"], capture_output=True, check=False
    )
    assert res.returncode == 2
