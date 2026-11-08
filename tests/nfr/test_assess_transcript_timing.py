"""NFR-021 transcript timing on the compose stack (plan assess-transcript-delivery 10b).

``scripts/assess_transcript_timing.py`` reads a stack run (at least 50 sessions closed
concurrently, 2 configured recipients each, the worker's relay a TLS mail sink that records
Message-IDs) and judges it: p99(generated - closed) <= 60 s, p95(sent - closed) <= 5 min with
SMTP healthy, >= 99% SENT or downloadable within 15 min. Without the run's environment
(``VEKTRA_NFR_ASSESS_DSN``, ``VEKTRA_NFR_ASSESS_SINCE``, ``VEKTRA_NFR_ASSESS_SINK_IDS``) it writes a
BLOCKED result file and reports SKIPPED: it never passes on a missing environment, and a run with
fewer than 50 closed sessions, or a SENT row the sink does not hold, is a FAIL. The script's logic
is unit-tested in ``vektra-assess/tests/test_transcript_timing_script.py``. Operator run: listed
in ``EXPECTED_UNRUN`` of the suite-coverage guard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_transcript_timing.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_transcript_timing", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


timing = _load()


@pytest.mark.integration
async def test_nfr_021_transcript_timing() -> None:
    cfg = timing.Config.from_env()
    doc, path = await timing.amain(cfg)
    if doc["status"] == "BLOCKED":
        pytest.skip(f"BLOCKED ({path.name}): " + "; ".join(doc["blockers"]))
    assert doc["status"] == "PASS", doc["problems"]
