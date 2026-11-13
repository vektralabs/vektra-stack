"""The worker's ``delivery_retry`` tick: the erasure sweep cannot stop the rest (review of #213).

The tick retries transcript deliveries, rescues dead notification chains, alerts the operator on
overdue erasures (OQ-033) and reaps interrupted SMTP tests. The erasure sweep is the newest step
and the only one that reads the erasure requests: an exception in it is logged and the reaper
still runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from vektra_app import assess_worker


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def step(name: str, *, fail: bool = False) -> Any:
        async def run(*_: Any, **__: Any) -> None:
            seen.append(name)
            if fail:
                raise RuntimeError("boom")

        return run

    import vektra_assess.delivery.handler as handler
    import vektra_assess.delivery.notify as notify
    import vektra_assess.delivery.smtp_test as smtp_test
    import vektra_assess.privacy.erasure as erasure

    monkeypatch.setattr(handler, "delivery_retry_tick", step("retry"))
    monkeypatch.setattr(notify, "rescue_dead_chains", step("rescue"))
    monkeypatch.setattr(erasure, "alert_overdue_erasures", step("erasure", fail=True))
    monkeypatch.setattr(smtp_test, "reap_interrupted_tests", step("reap"))
    return seen


async def test_a_failing_erasure_sweep_still_lets_the_reaper_run(
    calls: list[str],
) -> None:
    tick = assess_worker.make_delivery_retry_tick(
        object(),
        stale_after=timedelta(seconds=60),
        window=timedelta(hours=1),
        smtp_test_lease=timedelta(seconds=60),
        erasure_alert_after_s=3600,
    )
    await tick()  # never raises for the sweep
    assert calls == ["retry", "rescue", "erasure", "reap"]
