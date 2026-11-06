"""``make eval-assess``: the staging-tier run of the assess suite (plan assess-llm-runner, task E).

Runs every probe set through the real runner with the configured discussion model, scores the
delivered output (deterministic scorers + calibrated judge), reruns a subset 3 times for
determinism, and writes the activation report. Exit code 0 only on ``status: PASS``; BLOCKED and
FAIL exit non-zero, so a missing model, judge, calibration or dataset is never reported as a pass.

Environment: the ``VEKTRA_ASSESS_LLM_*`` settings of the discussion model,
``VEKTRA_ASSESS_PROVIDER_LOCALITY``, and ``VEKTRA_ASSESS_JUDGE_MODEL`` (+ optional
``VEKTRA_ASSESS_JUDGE_API_BASE``) for the judge.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import assess_adapter as A
import assess_determinism as D
import assess_judge as J
import assess_report as R
import assess_scoring as S

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.payload import bundle_to_model_payload
from vektra_shared.protocols import LLMProvider

#: NFR-022 minimum sizes; a smaller set makes the run BLOCKED
MINIMUMS = {
    "explain": 100,
    "refusal": 140,
    "yesno": 20,
    "oracle": 20,
    "contest": 90,
    "injection": 30,
}


async def _reachable(llm: LLMProvider) -> bool:
    try:
        return (await llm.health_check()).status == "healthy"
    except Exception:
        return False


async def evaluate(
    settings: VektraAssessSettings,
    *,
    llm: LLMProvider | None = None,
    judge_llm: LLMProvider | None = None,
    judge_model: str | None = None,
    root: Path = A.ROOT,
    determinism_items: int = 30,
) -> tuple[R.Report, list[str]]:
    manifest = A.load_manifest(root)
    A.check_data_basis(manifest, settings)  # raises before any request
    blocked: list[str] = []
    for name, minimum in MINIMUMS.items():
        if manifest.get("sizes", {}).get(name, 0) < minimum:
            blocked.append(f"dataset {name} under the NFR-022 minimum ({minimum})")
    runner = A.build_runner(settings, **({"llm": llm} if llm else {}))
    if not await _reachable(runner._llm):
        blocked.append(f"discussion model {settings.llm_model} unreachable")
        report = _empty(settings, runner.prompt_version, manifest, blocked)
        return report.finalize(), []
    fixtures = A.load_fixtures(root)
    items = [
        i for p in sorted((root / "probes").glob("*.jsonl")) for i in A.load_items(p)
    ]
    delivered = [await A.run_item(runner, fixtures[i.fixture], i) for i in items]
    scored = [S.score(d, fixtures[d.item.fixture]) for d in delivered]

    judged: dict[str, dict[str, bool]] = {}
    if judge_llm is not None and judge_model and await _reachable(judge_llm):
        for d in delivered:
            ctx = bundle_to_model_payload(fixtures[d.item.fixture].bundle)[0].content
            v = await J.judge(
                judge_llm, judge_model, d.item.id, ctx, d.item.text, d.text
            )
            judged[d.item.id] = dict(v.violations)
        for row in J.load_seeded(root / "calibration" / "seeded.jsonl"):
            ctx = bundle_to_model_payload(fixtures[row["fixture"]].bundle)[0].content
            v = await J.judge(
                judge_llm, judge_model, row["id"], ctx, row["question"], row["reply"]
            )
            judged[row["id"]] = dict(v.violations)
    else:
        blocked.append("judge model unset or unreachable")
    cal = J.calibrate(
        judged,
        J.load_human_labels(root / "calibration" / "human_labels.jsonl"),
        J.load_seeded(root / "calibration" / "seeded.jsonl"),
    )
    judged_hard = {
        k: sum(bool(judged.get(d.item.id, {}).get(k)) for d in delivered)
        for k in J.JUDGED_HARD
    }
    det_items = [i for i in items if i.kind == "turn"][:determinism_items]
    det = D.measure(await D.rerun(runner, fixtures, det_items, n=3), fixtures)
    rates = _rates(delivered, judged)
    report = R.Report(
        provider=(settings.llm_model or "").split("/")[0],
        model=settings.llm_model or "",
        prompt_version=runner.prompt_version,
        judge_model=judge_model,
        judge_prompt_version=J.JUDGE_PROMPT_VERSION,
        data_basis=manifest.get("data_basis", ""),
        dataset_sizes=manifest.get("sizes", {}),
        hard=S.tally(scored),
        judged_hard=judged_hard,
        rates=rates,
        calibration={
            "status": cal.status,
            "reasons": cal.reasons,
            "kappa": cal.kappa,
            "seeded_recall": cal.seeded_recall,
        },
        determinism={**asdict(det), "passed": det.passed},
        blocked_reasons=blocked,
    ).finalize()
    flagged = [
        i for i, v in judged.items() if any(v.values()) and not i.startswith("seeded-")
    ]
    contest = [d.item.id for d in delivered if d.item.category.startswith("contest:")]
    return report, R.review_queue(flagged, contest)


def _rates(
    delivered: Sequence[A.Delivered], judged: dict[str, dict[str, bool]]
) -> dict[str, Any]:
    def count(cat_prefix: str, crit: str) -> dict[str, Any]:
        ids = [d.item.id for d in delivered if d.item.category.startswith(cat_prefix)]
        hits = sum(bool(judged.get(i, {}).get(crit)) for i in ids)
        return R.rate(f"{crit} on {cat_prefix}", hits, len(ids) if judged else 0)

    return {
        "oracle": count("oracle", "oracle"),
        "over_refusal": count("explain", "over_refusal"),
        "wrong_refusal": count("refusal:", "wrong_refusal"),
        "contest_tag_missed": count("contest:", "contest_tag_missed"),
        "tone": count("", "tone"),
    }


def _empty(
    settings: VektraAssessSettings,
    pv: str,
    manifest: dict[str, Any],
    blocked: list[str],
) -> R.Report:
    return R.Report(
        provider=(settings.llm_model or "").split("/")[0], model=settings.llm_model or "",
        prompt_version=pv, judge_model=None, judge_prompt_version=J.JUDGE_PROMPT_VERSION,
        data_basis=manifest.get("data_basis", ""), dataset_sizes=manifest.get("sizes", {}),
        hard={k: 0 for k in S.HARD}, judged_hard={}, rates={},
        calibration={"status": "BLOCKED", "reasons": ["not run"]}, determinism=None,
        blocked_reasons=blocked,
    )  # fmt: skip


def _judge_llm() -> tuple[LLMProvider | None, str | None]:
    model = os.environ.get("VEKTRA_ASSESS_JUDGE_MODEL")
    if not model:
        return None, None
    from vektra_core.providers.litellm_provider import LitellmProvider
    from vektra_shared.config import LLMConfig

    cfg = LLMConfig(
        VEKTRA_LLM_PROVIDER=model,
        VEKTRA_LLM_API_BASE=os.environ.get("VEKTRA_ASSESS_JUDGE_API_BASE"),
        VEKTRA_LLM_FALLBACK_MODEL=None,
    )
    return LitellmProvider(cfg), model


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=R.RESULTS)
    args = p.parse_args(argv)
    settings = VektraAssessSettings()
    judge_llm, judge_model = _judge_llm()
    report, review = asyncio.run(
        evaluate(settings, judge_llm=judge_llm, judge_model=judge_model)
    )
    path = R.write(report, review, args.out)
    print(f"{report.status}: {path} sha256={report.report_sha256}")
    for reason in report.blocked_reasons + list(report.calibration.get("reasons", [])):
        print(f"  - {reason}")
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
