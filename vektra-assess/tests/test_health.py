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


# --- plan 12b: readiness details and the 503 marker ------------------------------------------

from types import SimpleNamespace  # noqa: E402

from vektra_assess.health import KEYS_MISSING, readiness_details  # noqa: E402

_GOOD = SimpleNamespace(
    site_id="x", retention_days=30, legal_basis_label="l", legal_basis_text="t",
    notice_url="https://uni.example/privacy", notice_text="n", notice_version="1",
)  # fmt: skip
_BAD = SimpleNamespace(**{**vars(_GOOD), "retention_days": 0})


def test_the_check_is_marked_as_a_readiness_probe() -> None:
    assert getattr(make_readiness_check(enabled=False), "readiness", False) is True


def test_details_are_figures_only() -> None:
    d = readiness_details(
        worker_age_s=3.04,
        row=(
            1000.0,
            {"state": "ready", "oldest_ready_age_s": 1.26, "worker_id": "w-1",
             "parser": {"ok": True, "at": 990.0, "error": None}},
            12.0,
            {"state": "busy", "last": {"ok": False}},
            {"ok": True, "last_success": 400.0, "rows_deleted": 7},
            2,
        ),
        privacy_rows=[_GOOD, _BAD],
        keys_present=True,
        parser_interval_s=60,
    )  # fmt: skip
    assert d == {
        "worker_heartbeat_age_s": 3.0,
        "worker_state": "ready",
        "oldest_ready_job_age_s": 1.3,
        "parser": {"status": "ok", "age_s": 10.0},
        "smtp_probe": {"status": "failed", "age_s": 12.0},  # busy: the previous verdict
        "privacy": {"active_sites": 2, "configured_sites": 1},
        "keys_present": True,
        "purge": {"last_success_age_s": 600.0, "last_run_ok": True},
    }
    assert "w-1" not in str(d)  # the worker id stays in the row


def test_an_old_parser_status_is_stale_and_none_is_unknown() -> None:
    old = (1000.0, {"parser": {"ok": True, "at": 700.0}}, None, None, None, 0)
    d = readiness_details(
        worker_age_s=None,
        row=old,
        privacy_rows=[],
        keys_present=True,
        parser_interval_s=60,
    )
    assert d["parser"] == {"status": "stale", "age_s": 300.0}
    d = readiness_details(
        worker_age_s=None,
        row=None,
        privacy_rows=[],
        keys_present=False,
        parser_interval_s=60,
    )
    assert d["parser"]["status"] == "unknown" and d["smtp_probe"]["status"] == "unknown"
    assert d["purge"] == {"last_success_age_s": None, "last_run_ok": None}


async def test_missing_api_keys_make_a_healthy_worker_not_ready() -> None:
    check = make_readiness_check(
        enabled=True, get_engine=lambda: FakeEngine(age=1.0), keys_present=lambda: False
    )
    res = await check()
    assert (res.status, res.message) == ("degraded", KEYS_MISSING)
    assert res.details is not None and res.details["keys_present"] is False
