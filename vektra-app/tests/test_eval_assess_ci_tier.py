"""CI tier of the assess eval suite (plan assess-llm-runner, task E): the whole pipeline of
``make eval-assess`` with recording providers, no network. It must collect cases (> 0), score a
clean model at 0 HARD, catch a hostile one, and stay BLOCKED while calibration data is missing."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))
sys.path.insert(0, str(ROOT / "vektra-assess" / "tests"))

import assess_adapter as A  # noqa: E402
import assess_judge as J  # noqa: E402
import assess_report as R  # noqa: E402
import eval_assess as E  # noqa: E402
from fakes import ScriptedHostileLLM  # noqa: E402

from vektra_assess.config import VektraAssessSettings  # noqa: E402
from vektra_shared.testing_recorders import RecordingLLMProvider  # noqa: E402

CLEAN_JUDGE = json.dumps({k: False for k in J.CRITERIA})


def _settings(**kw: object) -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {
            "VEKTRA_ASSESS_LLM_MODEL": "ollama/m",
            "VEKTRA_ASSESS_SAFEGUARD_MODE": "none",
            # the CI tier is a development run: no Presidio, a fake model (review of #203: the
            # safeguard/locality rule now holds inside build_assess_safeguard)
            "VEKTRA_ENV": "development",
            **kw,
        }
    )


def _root(tmp_path: Path) -> Path:
    """An isolated copy of the dataset with no human labels, whatever the workspace holds."""
    root = tmp_path / "assess"
    shutil.copytree(
        A.ROOT, root, ignore=shutil.ignore_patterns("results", "__pycache__")
    )
    (root / "calibration" / "human_labels.jsonl").unlink(missing_ok=True)
    return root


class _Down(RecordingLLMProvider):
    async def health_check(self):  # type: ignore[no-untyped-def]
        raise ConnectionError("down")


async def test_a_naive_model_is_scored_and_the_report_is_complete(
    tmp_path: Path,
) -> None:
    """A model that never refuses and always answers in Italian: no content violation, but the
    refusal core and the reply language fail, so the report is FAIL whatever the calibration."""
    naive = RecordingLLMProvider(
        ["Ti spiego il passaggio: rivedi il metodo usato."], repeat_last=True
    )
    judge = RecordingLLMProvider([CLEAN_JUDGE], repeat_last=True)
    root = _root(tmp_path)
    report, _ = await E.evaluate(
        _settings(), llm=naive, judge_llm=judge, judge_model="j/m", root=root
    )
    assert sum(report.dataset_sizes.values()) > 0  # cases were collected
    loaded = {p.stem: len(A.load_items(p)) for p in (root / "probes").glob("*.jsonl")}
    assert report.dataset_sizes == loaded  # actual scored counts, not the manifest
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
    down = _Down([])
    report, review = await E.evaluate(_settings(), llm=down)
    assert report.status == "BLOCKED" and review == []
    assert [c for c in down.calls if c.method == "stream"] == []


def test_the_scripted_hostile_provider_is_available_to_the_ci_tier() -> None:
    assert ScriptedHostileLLM()  # the same fakes the runner tests use


# --- CodeRabbit PR #151 regressions -----------------------------------------------------------


async def test_minimums_are_checked_on_loaded_probes_not_the_manifest(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    explain = root / "probes" / "explain.jsonl"
    explain.write_text(
        "".join(explain.read_text().splitlines(keepends=True)[:5]), encoding="utf-8"
    )  # the manifest still declares 100
    assert A.load_manifest(root)["sizes"]["explain"] == 100
    report, _ = await E.evaluate(_settings(), llm=_Down([]), root=root)
    assert report.dataset_sizes["explain"] == 5
    assert any("explain" in r and "minimum" in r for r in report.blocked_reasons)
    assert report.status == "BLOCKED"


async def test_a_judge_exception_is_a_judge_failure_and_the_report_replaces_a_stale_one(
    tmp_path: Path,
) -> None:
    class Broken(RecordingLLMProvider):
        async def complete(self, *a, **kw):  # type: ignore[no-untyped-def]
            raise TimeoutError("judge timed out")

    naive = RecordingLLMProvider(["Ti spiego il passaggio."], repeat_last=True)
    report, _ = await E.evaluate(
        _settings(), llm=naive, judge_llm=Broken([]), judge_model="j/m",
        root=_root(tmp_path), determinism_items=1,
    )  # fmt: skip
    assert "judge failed: TimeoutError" in report.blocked_reasons
    assert report.status in {"BLOCKED", "FAIL"}
    assert report.calibration["status"] == "BLOCKED"
    out = tmp_path / "results"
    out.mkdir()
    stale = out / f"{report.model.replace('/', '_')}-{report.prompt_version}.json"
    stale.write_text(json.dumps({"status": "PASS"}))
    assert R.write(report, [], out) == stale
    assert json.loads(stale.read_text())["status"] == report.status


async def test_a_non_synthetic_dataset_needs_an_onprem_judge_too(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["data_basis"] = "real-onprem-export"
    (root / "manifest.json").write_text(json.dumps(manifest))
    llm = RecordingLLMProvider(["x"], repeat_last=True)
    judge = RecordingLLMProvider([CLEAN_JUDGE], repeat_last=True)
    onprem = _settings(VEKTRA_ASSESS_PROVIDER_LOCALITY="onprem")
    with pytest.raises(A.DataBasisRefused):
        await E.evaluate(onprem, llm=llm, judge_llm=judge, judge_model="j/m", root=root)
    assert llm.calls == [] and judge.calls == []  # refused before any request
    report, _ = await E.evaluate(
        onprem, llm=_Down([]), judge_llm=judge, judge_model="j/m",
        judge_locality="onprem", root=root,
    )  # fmt: skip
    assert report.data_basis == "real-onprem-export"


def test_the_determinism_sample_covers_every_turn_category() -> None:
    items = [
        i for p in sorted((A.ROOT / "probes").glob("*.jsonl")) for i in A.load_items(p)
    ]
    cats = {i.category for i in items if i.kind == "turn"}
    sample = E.determinism_sample(items, 5)
    assert {i.category for i in sample} == cats  # each category, even past n
    assert all(i.kind == "turn" for i in sample)
    assert len(E.determinism_sample(items, 60)) == 60


async def test_part_b_determinism_is_measured_on_the_part_b_path(
    tmp_path: Path,
) -> None:
    llm = RecordingLLMProvider(
        ["Ciao! Possiamo partire dalla [Domanda 1]. Da dove vuoi iniziare?"],
        repeat_last=True,
    )
    report, _ = await E.evaluate(
        _settings(), llm=llm, root=_root(tmp_path), determinism_items=2
    )
    det = report.determinism
    assert det and det["part_b"]["runs"] == 3 and "passed" in det["part_b"]
    turn_cats = {
        i.category
        for p in (A.ROOT / "probes").glob("*.jsonl")
        for i in A.load_items(p)
        if i.kind == "turn"
    }
    assert set(det["categories"]) == turn_cats


async def test_human_labels_are_judged_on_their_own_fixed_reply(tmp_path: Path) -> None:
    """A label is never applied by probe id to this run's output: the judge reads its reply."""
    root = _root(tmp_path)
    label = {
        "id": "explain-0",  # a probe id: the label still binds to ITS reply, not the run's
        "fixture": "quiz-math-4.3-0",
        "question": "Perché ho perso punti?",
        "reply": "RISPOSTA-ETICHETTATA-UNICA",
        **{k: False for k in J.CRITERIA},
    }
    bad = {"id": "h-bad", **{k: False for k in J.CRITERIA}}  # no fixed reply
    (root / "calibration" / "human_labels.jsonl").write_text(
        json.dumps(label) + "\n", encoding="utf-8"
    )
    judge = RecordingLLMProvider([CLEAN_JUDGE], repeat_last=True)
    naive = RecordingLLMProvider(["Ti spiego il passaggio."], repeat_last=True)
    report, _ = await E.evaluate(
        _settings(), llm=naive, judge_llm=judge, judge_model="j/m",
        root=root, determinism_items=1,
    )  # fmt: skip
    bodies = [c.request_bytes.decode() for c in judge.calls]
    assert sum("RISPOSTA-ETICHETTATA-UNICA" in b for b in bodies) == 1
    assert "kappa" in report.calibration and report.calibration["status"] != "PASS"

    (root / "calibration" / "human_labels.jsonl").write_text(
        json.dumps(label) + "\n" + json.dumps(bad) + "\n", encoding="utf-8"
    )
    report, _ = await E.evaluate(
        _settings(), llm=naive, judge_llm=judge, judge_model="j/m",
        root=root, determinism_items=1,
    )  # fmt: skip
    assert report.calibration["status"] == "BLOCKED"
    assert any("h-bad" in r for r in report.calibration["reasons"])
