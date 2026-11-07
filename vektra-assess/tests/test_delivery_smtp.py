"""Plan 10b ``delivery/smtp.py`` against the in-process TLS mail sink.

Every "sent" is checked on the sink; every refusal is checked to have left 0 messages there and,
where relevant, 0 AUTH attempts in plaintext.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures.mailsink import HOST, MailSink, sink_config

from vektra_assess.delivery import message as m
from vektra_assess.delivery.smtp import ErrorClass, SmtpSender


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[MailSink]:
    s = MailSink(tmp_path / "sink").start()
    yield s
    s.stop()


def _msg(n: int = 0, to: str = "prof@unical.it") -> EmailMessage:
    return m.build(
        m.TranscriptMail(
            sender="vektra@vektra.example.org",
            recipient=to,
            course="EAASSESS",
            cmid=501,
            student_userid=101,
            session_no=1,
            filename="EAASSESS_501_101_s1_20261107.html",
            attachment=b"<!DOCTYPE html><html lang=it></html>",
            message_id=m.message_id("t", 1, f"{n:064d}", "vektra.example.org"),
            date=datetime.now(UTC),
        )
    )


async def test_a_healthy_send_is_on_the_sink_over_tls_with_auth(sink: MailSink) -> None:
    sender = SmtpSender(sink_config(sink))
    msg = _msg()
    res = await sender.send(msg, "prof@unical.it")
    assert res.ok and res.smtp_code == 250
    got = sink.by_message_id(str(msg["Message-ID"]))
    assert len(got) == 1 and got[0].tls and got[0].authenticated
    assert got[0].rcpt == ("prof@unical.it",)
    assert sink.auth_attempts == [True]  # AUTH only after TLS


async def test_a_relay_without_starttls_gets_nothing_not_even_auth(
    tmp_path: Path,
) -> None:
    plain = MailSink(tmp_path / "plain", starttls=False, require_auth=False).start()
    try:
        res = await SmtpSender(sink_config(plain)).send(_msg(), "prof@unical.it")
        assert not res.ok and res.error_class is ErrorClass.TLS and res.retryable
        assert plain.messages == [] and plain.auth_attempts == []
    finally:
        plain.stop()


async def test_an_untrusted_certificate_is_a_tls_failure(
    sink: MailSink, tmp_path: Path
) -> None:
    other = MailSink(tmp_path / "other")  # a different CA, never started
    res = await SmtpSender(sink_config(sink, ca_file=other.ca_file)).send(
        _msg(), "prof@unical.it"
    )
    assert res.error_class is ErrorClass.TLS
    assert sink.messages == [] and sink.auth_attempts == []


@pytest.mark.parametrize(
    ("fault", "cls", "code", "retryable"),
    [
        ("4xx", ErrorClass.TEMP_4XX, 451, True),
        ("5xx", ErrorClass.PERM_5XX, 550, False),
    ],
)
async def test_rcpt_replies_are_classified(
    sink: MailSink, fault: str, cls: ErrorClass, code: int, retryable: bool
) -> None:
    sink.fault = fault
    res = await SmtpSender(sink_config(sink)).send(_msg(), "prof@unical.it")
    assert (res.ok, res.error_class, res.smtp_code, res.retryable) == (
        False,
        cls,
        code,
        retryable,
    )
    assert sink.messages == []


async def test_a_relay_that_is_down_is_a_connect_failure(sink: MailSink) -> None:
    sink.down()
    res = await SmtpSender(sink_config(sink)).send(_msg(), "prof@unical.it")
    assert res.error_class is ErrorClass.CONNECT and res.retryable
    sink.up()
    assert (await SmtpSender(sink_config(sink)).send(_msg(), "prof@unical.it")).ok


async def test_a_drop_after_data_is_not_a_success(sink: MailSink) -> None:
    """The relay holds the message but its 250 never arrives: not SENT (REQ-075)."""
    sink.fault = "drop_after_data"
    msg = _msg()
    res = await SmtpSender(sink_config(sink)).send(msg, "prof@unical.it")
    assert not res.ok and res.error_class is ErrorClass.CONNECT
    assert len(sink.by_message_id(str(msg["Message-ID"]))) == 1


async def test_refused_credentials_are_an_auth_failure(sink: MailSink) -> None:
    res = await SmtpSender(sink_config(sink, password="wrong")).send(
        _msg(), "prof@unical.it"
    )
    assert res.error_class is ErrorClass.AUTH and sink.messages == []
    # every mechanism the client tried (PLAIN, LOGIN) ran over TLS
    assert sink.auth_attempts and all(sink.auth_attempts)


async def test_concurrent_sessions_never_pass_the_cap(sink: MailSink) -> None:
    sender = SmtpSender(sink_config(sink, max_connections=2))
    msgs = [_msg(i) for i in range(6)]
    results = await asyncio.gather(*(sender.send(x, "prof@unical.it") for x in msgs))
    assert all(r.ok for r in results)
    assert sender.peak_sessions == 2  # the cap was reached and never passed
    assert len(sink.messages) == 6


async def test_a_silent_relay_times_out() -> None:
    """A listener that accepts and never speaks: the hard timeout ends the attempt."""
    srv = socket.socket()
    srv.bind((HOST, 0))
    srv.listen(1)
    try:
        cfg = sink_config(
            SimpleNamespace(port=srv.getsockname()[1], ca_file=None),  # type: ignore[arg-type]
            timeout_s=0.5,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        res = await SmtpSender(cfg).send(_msg(), "prof@unical.it")
        assert res.error_class is ErrorClass.CONNECT
        assert loop.time() - started < 3
    finally:
        srv.close()


async def test_the_probe_never_sends(sink: MailSink, tmp_path: Path) -> None:
    sender = SmtpSender(sink_config(sink))
    assert (await sender.probe()).ok
    assert sink.messages == [] and sink.auth_attempts == [True]
    plain = MailSink(tmp_path / "plain", starttls=False, require_auth=False).start()
    try:
        res = await SmtpSender(sink_config(plain)).probe()
        assert res.error_class is ErrorClass.TLS and plain.messages == []
    finally:
        plain.stop()


# --- review fixes: QUIT after the 250, and CONNECT vs TLS --------------------------------


async def test_a_quit_that_never_answers_after_the_250_is_still_sent(
    sink: MailSink,
) -> None:
    """Accepted after the 250 to DATA means SENT: QUIT is best effort, outside the send's
    timeout, with its own short bound (a resend here would be a real duplicate)."""
    sink.fault = "slow_quit"
    sender = SmtpSender(sink_config(sink, timeout_s=1.0))
    sender.quit_timeout_s = 0.2
    loop = asyncio.get_running_loop()
    started = loop.time()
    res = await sender.send(_msg(), "prof@unical.it")
    assert res.ok and res.smtp_code == 250
    assert len(sink.messages) == 1
    assert loop.time() - started < 1.0  # the QUIT bound, not the send timeout, ended it


async def test_a_refused_connection_is_connect_in_both_tls_modes(
    sink: MailSink,
) -> None:
    port = sink.port
    sink.down()
    for mode in ("starttls", "implicit"):
        res = await SmtpSender(sink_config(sink, tls_mode=mode, port=port)).send(
            _msg(), "prof@unical.it"
        )
        assert res.error_class is ErrorClass.CONNECT, mode


async def test_a_drop_during_the_plaintext_ehlo_is_connect_not_tls(
    sink: MailSink,
) -> None:
    sink.fault = "drop_on_ehlo"
    res = await SmtpSender(sink_config(sink)).send(_msg(), "prof@unical.it")
    assert res.error_class is ErrorClass.CONNECT and sink.messages == []


async def test_implicit_tls_sends_and_an_untrusted_certificate_is_tls(
    tmp_path: Path,
) -> None:
    sink = MailSink(tmp_path / "465", implicit=True).start()
    other = MailSink(tmp_path / "other-ca")
    try:
        ok = await SmtpSender(sink_config(sink, tls_mode="implicit")).send(
            _msg(), "prof@unical.it"
        )
        assert ok.ok and len(sink.messages) == 1
        bad = await SmtpSender(
            sink_config(sink, tls_mode="implicit", ca_file=other.ca_file)
        ).send(_msg(1), "prof@unical.it")
        assert bad.error_class is ErrorClass.TLS and len(sink.messages) == 1
    finally:
        sink.stop()
