"""/health/assess readiness from the worker heartbeat (plan assess-data-crypto 8.1)."""

from __future__ import annotations

from vektra_assess.health import make_readiness_check


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Conn:
    def __init__(self, value, boom):
        self._value, self._boom = value, boom

    async def __aenter__(self):
        if self._boom:
            raise ConnectionError("db down")
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _sql, _params):
        return _Result(self._value)


class FakeEngine:
    def __init__(self, age=None, boom=False):
        self._age, self._boom = age, boom

    def connect(self):
        return _Conn(self._age, self._boom)


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
