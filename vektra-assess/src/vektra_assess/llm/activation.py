"""NFR-023 gate: which (provider, model, prompt_version) may answer students.

``evaluate_report`` recomputes the verdict of an eval report (``make eval-assess`` JSON) instead of
trusting its ``status`` field: the SHA-256 must match the canonical body, every HARD class must be
0 (deterministic and judge-based), the judge calibration must have PASSED and the data basis must
be declared. ``is_activated`` asks the database for a passing row of the triple. Nothing here
writes: the admin endpoint appends rows, startup and the Part B pre-flight read them.

``canonical_json`` is the one canonical form of a report; ``tests/eval/assess_report.py`` hashes
with it too, so the hash the eval suite prints is the hash this gate recomputes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# content-free refusal codes (the audit row carries them, never report content)
SHA_MISMATCH = "report_sha_mismatch"
NOT_PASS = "report_status_not_pass"
HARD_FAILED = "hard_class_failed"
JUDGE_HARD_FAILED = "judge_hard_class_failed"
UNCALIBRATED = "judge_calibration_not_passed"
NO_DATA_BASIS = "data_basis_missing"
MALFORMED = "report_malformed"
TRIPLE_MISMATCH = "report_triple_mismatch"


def canonical_json(doc: Any) -> bytes:
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def report_sha256(doc: Mapping[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "report_sha256"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


@dataclass(frozen=True)
class Evaluation:
    accepted: bool
    reasons: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    sha256: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


_HEX8 = re.compile(r"^[0-9a-f]{8}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def evaluate_report(
    doc: Mapping[str, Any], *, expected_prompt_version: str | None = None
) -> Evaluation:
    """Never raises: any malformed report is a refusal with ``report_malformed``, so the endpoint
    always answers ERR-ASSESS-004 and always writes its audit row."""
    try:
        provider, model = str(doc["provider"]), str(doc["model"])
        pv, sha = str(doc["prompt_version"]), str(doc["report_sha256"])
        hard = {str(k): int(v) for k, v in dict(doc["hard"]).items()}
        judged = {str(k): int(v) for k, v in dict(doc["judged_hard"]).items()}
        calibration = dict(doc["calibration"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return Evaluation(False, (MALFORMED,))
    if not provider or not model or not _HEX8.match(pv) or not _HEX64.match(sha):
        # the same shapes the table CHECKs enforce: refused here, never a 500 at INSERT
        return Evaluation(False, (MALFORMED,), provider, model[:200], pv[:8], sha[:64])
    reasons: list[str] = []
    if report_sha256(doc) != sha:
        reasons.append(SHA_MISMATCH)
    if doc.get("status") != "PASS":
        reasons.append(NOT_PASS)
    if not hard or any(v != 0 for v in hard.values()):
        reasons.append(HARD_FAILED)
    if not judged or any(v != 0 for v in judged.values()):
        # an empty judge section means no judge-based HARD class was scored at all
        reasons.append(JUDGE_HARD_FAILED)
    if calibration.get("status") != "PASS":
        reasons.append(UNCALIBRATED)
    if not doc.get("data_basis"):
        reasons.append(NO_DATA_BASIS)
    if expected_prompt_version is not None and pv != expected_prompt_version:
        reasons.append(TRIPLE_MISMATCH)
    return Evaluation(not reasons, tuple(reasons), provider, model, pv, sha)


async def is_activated(conn: AsyncConnection, model: str, prompt_version: str) -> bool:
    row = (
        await conn.execute(
            text(
                "SELECT 1 FROM assess_model_activations "
                "WHERE model = :m AND prompt_version = :p AND hard_pass LIMIT 1"
            ),
            {"m": model, "p": prompt_version},
        )
    ).first()
    return row is not None
