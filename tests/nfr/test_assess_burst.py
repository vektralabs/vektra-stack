"""Early Start burst, the real run (plan assess-session-lifecycle M1.11; re-run at M5.12).

``test_burst_40_concurrent_starts`` fires the burst of ``scripts/assess_burst.py`` against the
configured dev stack, dev Moodle and target model (``VEKTRA_ASSESS_BURST_*``, see the script).
Without them, or when any is unreachable, it writes a BLOCKED result file and reports SKIPPED: it
never passes on a missing environment. It is an operator run (it reaches the target model through
the API under test), listed in ``EXPECTED_UNRUN`` of the suite-coverage guard. The script's own
logic is unit-tested in ``vektra-assess/tests/test_burst_script.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_burst.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_burst", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


burst = _load()


@pytest.mark.integration
async def test_burst_40_concurrent_starts() -> None:
    """The real burst. SKIPPED (and a BLOCKED result file) unless the environment is configured."""
    cfg = burst.Config.from_env()
    reasons = await burst.blockers(cfg)
    if reasons:
        path = burst.write_result(burst.blocked_result(reasons, cfg))
        pytest.skip(f"BLOCKED ({path.name}): " + "; ".join(reasons))
    code, path = await burst.amain(cfg)
    doc = json.loads(path.read_text())
    assert burst.validate_result(doc) == [], doc.get("summary")
    # NFR-018 is a should: a breach is reported in the result file per model, not hidden
    assert code in (0, 1), path
