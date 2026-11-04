"""/health/assess readiness from the worker heartbeat (plan assess-data-crypto 8.1)."""

from __future__ import annotations

from vektra_assess.health import make_readiness_check


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Conn:
    def __init__(self, row, boom):
        self._row, self._boom = row, boom

    async def __aenter__(self):
        if self._boom:
            raise ConnectionError("db down")
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _sql, _params):
        return _Result(self._row)


class FakeEngine:
    def __init__(self, age=None, boom=False, state=None, backlog=None):
        self._row = None if age is None else (age, state, backlog)
        self._boom = boom

    def connect(self):
        return _Conn(self._row, self._boom)


async def _status(engine, max_age_s=60):
    check = make_readiness_check(
        enabled=True, get_engine=lambda: engine, max_age_s=max_age_s
    )
    return await check()


async def test_fresh_heartbeat_is_healthy():
    assert (await _status(FakeEngine(age=3.0))).status == "healthy"


async def test_stale_heartbeat_is_degraded():
    res = await _status(FakeEngine(age=61.0))
    assert (res.status, res.message) == ("degraded", "heartbeat_stale")
    assert (
        await _status(FakeEngine(age=20.0), max_age_s=10)
    ).message == "heartbeat_stale"


async def test_no_row_is_degraded():
    res = await _status(FakeEngine(age=None))
    assert (res.status, res.message) == ("degraded", "no_heartbeat")


async def test_unreachable_database_is_unhealthy_not_an_exception():
    res = await _status(FakeEngine(boom=True))
    assert (res.status, res.message) == ("unhealthy", "heartbeat_unreadable")


async def test_no_engine_wired_never_reports_healthy():
    check = make_readiness_check(enabled=True, get_engine=lambda: None)
    assert (await check()).message == "heartbeat_source_missing"


async def test_disabled_needs_no_engine():
    assert (await make_readiness_check(enabled=False)()).status == "disabled"


async def test_worker_state_other_than_ready_is_degraded():
    for state in ("starting", "draining", "stopped"):
        res = await _status(FakeEngine(age=1.0, state=state))
        assert (res.status, res.message) == ("degraded", "worker_not_ready"), state
    assert (await _status(FakeEngine(age=1.0, state="ready"))).status == "healthy"


async def test_old_ready_job_backlog_is_degraded_from_the_row_alone():
    res = await _status(FakeEngine(age=1.0, state="ready", backlog="31.0"))
    assert (res.status, res.message) == ("degraded", "job_backlog")
    assert (
        await _status(FakeEngine(age=1.0, state="ready", backlog="29.9"))
    ).status == "healthy"


async def test_a_row_without_the_optional_fields_is_judged_on_age_only():
    assert (await _status(FakeEngine(age=2.0))).status == "healthy"
