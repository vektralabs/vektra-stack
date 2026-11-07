"""Plan 10b ``delivery/probe.py``: the relay's readiness row for ``/health/assess``; never a send."""

from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.mailsink import MailSink, sink_config
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.delivery.probe import COMPONENT, run_probe
from vektra_assess.delivery.smtp import SmtpSender

pytestmark = pytest.mark.integration


async def _row(pg):  # type: ignore[no-untyped-def]
    got = await pg.run(
        "api",  # what /health/assess will read, as the API role
        "SELECT details, extract(epoch FROM now() - beat_at) FROM assess_heartbeat "
        "WHERE component = :c",
        {"c": COMPONENT},
    )
    return got[0] if got else None


async def test_the_probe_writes_its_status_row_and_never_sends(
    pg, tmp_path: Path, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(pg.url("worker"))
    sink = MailSink(tmp_path / "ok").start()
    plain = MailSink(tmp_path / "plain", starttls=False, require_auth=False).start()
    try:
        res = await run_probe(SmtpSender(sink_config(sink)), engine.connect)
        assert res.ok
        details, age = await _row(pg)
        assert details == {"ok": True, "error_class": None, "smtp_code": None}
        assert float(age) < 60
        assert sink.messages == [] and sink.auth_attempts and all(sink.auth_attempts)
        res = await run_probe(SmtpSender(sink_config(plain)), engine.connect)
        assert not res.ok
        details, _ = await _row(pg)
        assert details == {"ok": False, "error_class": "tls", "smtp_code": None}
        assert plain.messages == [] and plain.auth_attempts == []
        # the worker's own heartbeat row is untouched by the probe
        assert (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_heartbeat WHERE component = 'worker' "
                "AND details ? 'error_class'",
            )
        )[0][0] == 0
    finally:
        sink.stop()
        plain.stop()
        await engine.dispose()
    assert "smtp_probe_tls" in canary_log_capture.text()
    canary_log_capture.assert_clean([sink_config(sink).host + ":" + str(sink.port)])
