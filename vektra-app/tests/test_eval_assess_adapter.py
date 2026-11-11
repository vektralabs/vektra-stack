"""The assess eval adapter (plan assess-llm-runner, task E), on the CI tier: no network."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "eval"))

import assess_adapter as A  # noqa: E402

from vektra_assess.config import VektraAssessSettings  # noqa: E402
from vektra_shared.safeguards import PassthroughSafeguard  # noqa: E402
from vektra_shared.testing_recorders import RecordingLLMProvider  # noqa: E402


def _settings(**kw: object) -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_LLM_MODEL": "t/m", "VEKTRA_ASSESS_SAFEGUARD_MODE": "none", **kw}
    )


def test_the_eval_path_refuses_no_safeguard_with_a_cloud_model() -> None:
    """Review of #203: the eval adapter builds the safeguard itself, not through startup step
    13, so the safeguard/locality rule must hold inside ``build_assess_safeguard``."""
    from vektra_shared.startup import StartupValidationError

    llm = RecordingLLMProvider(["x"])
    for env in ({}, {"VEKTRA_ENV": "production"}):  # none, cloud, unset or production
        with pytest.raises(StartupValidationError, match="LOCALITY=cloud") as e:
            A.build_runner(_settings(**env), llm=llm)
        assert "safeguard is required" in e.value.remediation
    dev = A.build_runner(_settings(VEKTRA_ENV="development"), llm=llm)
    onprem = A.build_runner(
        _settings(VEKTRA_ENV="production", VEKTRA_ASSESS_PROVIDER_LOCALITY="onprem"),
        llm=llm,
    )
    assert dev and onprem


def test_fixtures_load_only_through_assemble_context() -> None:
    fx = A.load_fixtures()
    assert "quiz-math-4.3-0" in fx
    assert type(fx["quiz-math-4.3-0"].bundle).__name__ == "ContextBundle"


async def test_the_dataset_runs_through_the_real_runner() -> None:
    llm = RecordingLLMProvider(
        ["Nella [Domanda 2] il commento indica il segno. Ne parliamo?",
         "Ciao! Possiamo partire dalla [Domanda 2]. Da dove vuoi iniziare?"]
    )  # fmt: skip
    settings = _settings()
    runner = A.build_runner(settings, llm=llm, safeguard=PassthroughSafeguard())
    items = A.load_items(A.ROOT / "smoke.jsonl")
    out = await A.run_dataset(runner, settings, items)
    assert [d.item.id for d in out] == ["smoke-explain-01", "smoke-partb-01"]
    assert out[0].segments and out[1].text.startswith("Ciao!")
    assert len([c for c in llm.calls if c.method == "stream"]) == 2


async def test_a_non_synthetic_dataset_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assess"
    shutil.copytree(A.ROOT, root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["data_basis"] = "real-onprem-export"
    (root / "manifest.json").write_text(json.dumps(manifest))
    llm = RecordingLLMProvider(["x"])
    settings = _settings()  # locality defaults to cloud
    assert settings.provider_locality == "cloud"
    runner = A.build_runner(settings, llm=llm, safeguard=PassthroughSafeguard())
    with pytest.raises(A.DataBasisRefused):
        await A.run_dataset(runner, settings, A.load_items(root / "smoke.jsonl"), root)
    assert llm.calls == []  # 0 requests


async def test_onprem_may_run_a_non_synthetic_dataset(tmp_path: Path) -> None:
    root = tmp_path / "assess"
    shutil.copytree(A.ROOT, root)
    (root / "manifest.json").write_text(
        json.dumps({"data_basis": "real-onprem-export"})
    )
    settings = _settings(VEKTRA_ASSESS_PROVIDER_LOCALITY="onprem")
    runner = A.build_runner(
        settings,
        llm=RecordingLLMProvider(["Ok."], repeat_last=True),
        safeguard=PassthroughSafeguard(),
    )
    out = await A.run_dataset(
        runner, settings, A.load_items(root / "smoke.jsonl"), root
    )
    assert len(out) == 2
