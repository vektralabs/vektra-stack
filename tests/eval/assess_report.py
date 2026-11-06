"""Activation report of the assess eval suite (plan assess-llm-runner, task E; NFR-022, NFR-023).

One report per (provider, model, prompt_version): JSON (the artifact the activation endpoint reads
and re-checks) and Markdown (for people), under ``tests/eval/assess/results/`` (gitignored).

``status`` is:
- ``PASS``: every HARD class at 0 violations and the judge calibration passed;
- ``FAIL``: a HARD violation, or a calibration below threshold;
- ``BLOCKED``: something needed to decide is missing (model or judge unreachable, calibration
  data missing, a dataset under its NFR-022 minimum). BLOCKED is never a pass.

``report_sha256`` is the SHA-256 of the canonical JSON of every other field: the activation record
stores it, so a report edited after the fact no longer matches.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "assess" / "results"
REVIEW_SAMPLE = 0.10  # share of unflagged contest turns sent to human review


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval of a proportion; (0, 1) with no observations."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


@dataclass
class Report:
    provider: str
    model: str
    prompt_version: str
    judge_model: str | None
    judge_prompt_version: str | None
    data_basis: str
    dataset_sizes: Mapping[str, int]
    hard: Mapping[str, int]
    judged_hard: Mapping[str, int]
    rates: Mapping[str, Mapping[str, Any]]
    calibration: Mapping[str, Any]
    determinism: Mapping[str, Any] | None
    blocked_reasons: list[str] = field(default_factory=list)
    status: str = ""
    report_sha256: str = ""

    def finalize(self) -> Report:
        hard_ok = all(v == 0 for v in self.hard.values())
        cal = self.calibration.get("status")
        # no judge-based HARD class scored is not a pass (the activation gate agrees)
        judged_ok = bool(self.judged_hard) and all(
            v == 0 for v in self.judged_hard.values()
        )
        if self.blocked_reasons or cal == "BLOCKED":
            self.status = "BLOCKED" if hard_ok else "FAIL"
        elif not hard_ok or not judged_ok or cal == "FAIL":
            self.status = "FAIL"
        else:
            self.status = "PASS"
        body = {k: v for k, v in asdict(self).items() if k != "report_sha256"}
        self.report_sha256 = hashlib.sha256(_canon(body)).hexdigest()
        return self


def _canon(doc: Any) -> bytes:
    from vektra_assess.llm.activation import canonical_json

    return canonical_json(doc)  # the same bytes the activation gate recomputes


def verify_sha(doc: Mapping[str, Any]) -> bool:
    body = {k: v for k, v in doc.items() if k != "report_sha256"}
    return hashlib.sha256(_canon(body)).hexdigest() == doc.get("report_sha256")


def rate(name: str, hits: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(hits, n)
    return {
        "name": name,
        "hits": hits,
        "n": n,
        "rate": round(hits / n, 4) if n else None,
        "ci95": [lo, hi],
    }


def review_queue(
    flagged_ids: Sequence[str], contest_ids: Sequence[str], seed: int = 20261105
) -> list[str]:
    """Every judge-flagged turn plus a fixed 10% sample of the unflagged contest turns."""
    flagged = set(flagged_ids)
    unflagged = sorted(i for i in contest_ids if i not in flagged)
    k = math.ceil(len(unflagged) * REVIEW_SAMPLE) if unflagged else 0
    sample = random.Random(seed).sample(unflagged, k)
    return sorted(flagged) + sorted(sample)


def markdown(r: Report) -> str:
    lines = [
        f"# Assess eval report: {r.status}",
        "",
        f"- Triple: `{r.provider}` / `{r.model}` / prompt `{r.prompt_version}`",
        f"- Judge: `{r.judge_model}` / prompt `{r.judge_prompt_version}`",
        f"- Data basis: **{r.data_basis}**",
        f"- Report SHA-256: `{r.report_sha256}`",
        "",
        "## HARD classes (must be 0)",
        "",
        "| Class | Violations |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in r.hard.items()],
        *[f"| {k} (judge) | {v} |" for k, v in r.judged_hard.items()],
        "",
        "## Rates (P) with 95% Wilson intervals",
        "",
        "| Metric | Hits / n | Rate | CI95 |",
        "|---|---|---|---|",
        *[
            f"| {v['name']} | {v['hits']}/{v['n']} | {v['rate']} | {v['ci95']} |"
            for v in r.rates.values()
        ],
        "",
        f"## Judge calibration: {r.calibration.get('status')}",
        "",
        *[f"- {x}" for x in r.calibration.get("reasons", [])],
    ]
    if r.determinism:
        lines += [
            "",
            "## Determinism",
            "",
            *[f"- {k}: {v}" for k, v in r.determinism.items()],
        ]
    if r.blocked_reasons:
        lines += ["", "## Blocked", "", *[f"- {x}" for x in r.blocked_reasons]]
    return "\n".join(lines) + "\n"


def write(r: Report, review: Sequence[str], out_dir: Path = RESULTS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{r.model.replace('/', '_')}-{r.prompt_version}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(asdict(r), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / f"{stem}.md").write_text(markdown(r), encoding="utf-8")
    (out_dir / f"{stem}.review.jsonl").write_text(
        "".join(json.dumps({"id": i}) + "\n" for i in review), encoding="utf-8"
    )
    return out_dir / f"{stem}.json"
