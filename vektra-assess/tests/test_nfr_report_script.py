"""Hermetic checks of ``scripts/assess_nfr_report.py`` (plan assess-hardening-release, 13a).

The judgement over a campaign run: percentiles, the per-item thresholds, the admitted-Start rule,
the concurrency sweep, and the false-pass guards. A sample below its minimum is BLOCKED, a missing
environment is BLOCKED, and first visible output is always BLOCKED. Reading the rows is covered by
``integration/test_nfr_report_db.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_nfr_report.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assess_nfr_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


r = _load()


def _good(n: int = 30) -> object:
    return r.Data(
        commit_s=[5.0] * n,
        reply_s=[12.0] * n,
        attempts=[("committed", None)] * n,
        close_reasons=["finished"] * n,
        live_intervals=[(0.0, 10.0), (5.0, 15.0)],
    )


def _item(result: dict, name: str) -> dict:  # type: ignore[type-arg]
    return next(i for i in result["items"] if i["item"] == name)


def test_percentile_is_nearest_rank() -> None:
    assert r.percentile([], 95) is None
    assert r.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert r.percentile([float(i) for i in range(1, 101)], 95) == 95.0


def test_first_visible_output_is_never_computed() -> None:
    res = r.evaluate(_good())
    assert _item(res, "NFR-018 first visible output")["verdict"] == "BLOCKED"
    # one BLOCKED item keeps the whole report from PASS
    assert res["verdict"] == "BLOCKED"


def test_every_computed_item_passes_on_a_good_run() -> None:
    res = r.evaluate(_good())
    computed = [i for i in res["items"] if i["item"] != "NFR-018 first visible output"]
    assert {i["verdict"] for i in computed} == {"PASS"}


def test_a_slow_commit_fails_on_max_even_with_good_percentiles() -> None:
    d = _good()
    d.commit_s = [5.0] * 29 + [61.0]
    res = r.evaluate(d)
    assert _item(res, "NFR-018 commit latency")["verdict"] == "FAIL"
    assert res["verdict"] == "FAIL"


def test_a_small_sample_is_blocked_not_judged() -> None:
    d = _good(n=5)
    res = r.evaluate(d)
    item = _item(res, "NFR-018 commit latency")
    assert (
        item["verdict"] == "BLOCKED" and "below the declared minimum" in item["reason"]
    )


def test_capacity_and_student_rollbacks_are_not_admitted() -> None:
    d = _good()
    d.attempts = [("committed", None)] * 30 + [
        ("rolled_back", "capacity"),
        ("refused", "capacity"),
        ("rolled_back", "part_b_rejected"),
        ("rolled_back", "terminated_by_teacher"),
    ]
    res = r.evaluate(d)
    item = _item(res, "NFR-019 commit rate")
    assert item["admitted"] == 30 and item["rate"] == 1.0 and item["verdict"] == "PASS"
    assert res["nfr_020_inputs"]["capacity_refusals"] == 2


def test_a_system_rollback_counts_against_the_commit_rate() -> None:
    d = _good(n=50)
    d.attempts = [("committed", None)] * 49 + [("rolled_back", "provider_unavailable")]
    item = _item(r.evaluate(d), "NFR-019 commit rate")
    assert item["rate"] == 0.98 and item["verdict"] == "FAIL"


def test_service_failure_rate_threshold() -> None:
    d = _good(n=100)
    d.close_reasons = ["finished"] * 97 + ["service_failure"] * 3
    assert _item(r.evaluate(d), "NFR-019 service_failure rate")["verdict"] == "FAIL"


def test_concurrency_sweep_counts_overlap() -> None:
    got = r.concurrency([(0.0, 10.0), (5.0, 15.0), (15.0, 20.0)])
    assert got["max_live"] == 2
    assert got["seconds_at_level"] == {"1": 15.0, "2": 5.0}


def test_a_missing_environment_is_blocked_and_written(tmp_path: Path) -> None:
    code, res = r.run(r.Config.from_env({}), out_dir=tmp_path)
    assert code == 2 and res["verdict"] == "BLOCKED"
    assert any("VEKTRA_NFR_ASSESS_DSN" in b for b in res["blockers"])
    files = sorted(tmp_path.iterdir())
    assert [f.suffix for f in files] == [".json", ".md"]
    assert json.loads(files[0].read_text())["verdict"] == "BLOCKED"


def test_an_unparsable_window_is_blocked() -> None:
    cfg = r.Config.from_env(
        {
            "VEKTRA_NFR_ASSESS_DSN": "postgresql://x",
            "VEKTRA_NFR_ASSESS_SINCE": "yesterday",
        }
    )
    assert any("not an ISO 8601" in b for b in cfg.blockers())
