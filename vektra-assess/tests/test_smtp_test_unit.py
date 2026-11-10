"""The operator's SMTP test send, without a database (plan 12b)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from vektra_assess.delivery import smtp_test as st
from vektra_assess.delivery.smtp import SendResult, SlotBusy


class _Res:
    def __init__(self, first: Any = None, rowcount: int = 1) -> None:
        self._first, self.rowcount = first, rowcount

    def first(self) -> Any:
        return self._first


class _Conn:
    def __init__(self, log: list[str], claim: bool) -> None:
        self.log, self.claim = log, claim

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    def begin(self) -> _Conn:
        return self

    async def execute(self, stmt: Any, params: Any = None) -> _Res:
        sql = str(stmt)
        self.log.append(sql.split()[0] + (" status" if "'FAILED'" in sql else ""))
        if "attempt_started_at = :now" in sql:
            return _Res(("id",) if self.claim else None)
        return _Res()


def _ctx(log: list[str], claim: bool = True, args: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=1,
        args={"test_id": str(uuid4())} if args is None else args,
        acquire=lambda: _Conn(log, claim),
    )


class _Sender:
    def __init__(self, result: SendResult | Exception) -> None:
        self.result = result
        self.sent: list[str] = []

    async def send(self, msg: Any, recipient: str) -> SendResult:
        self.sent.append(recipient)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _deps(sender: _Sender) -> st.SmtpTestDeps:
    return st.SmtpTestDeps(
        sender=sender,
        sender_address="vektra@vektra.example.org",
        message_id_domain="vektra.example.org",
        operator_address="ops@vektra.example.org",
    )


async def test_a_malformed_job_sends_nothing() -> None:
    s = _Sender(SendResult(True, None, 250))
    assert (
        await st.run_test(_deps(s), _ctx([], args={"test_id": "nope"})) == "malformed"
    )
    assert s.sent == []


async def test_a_busy_relay_is_a_failed_test_never_a_retry() -> None:
    s = _Sender(SlotBusy())
    log: list[str] = []
    assert await st.run_test(_deps(s), _ctx(log)) == st.BUSY
    assert any(entry.endswith("status") for entry in log)  # recorded FAILED


async def test_the_message_is_fixed_and_goes_to_the_operator_only() -> None:
    s = _Sender(SendResult(True, None, 250))
    assert await st.run_test(_deps(s), _ctx([])) == "sent"
    assert s.sent == ["ops@vektra.example.org"]
    msg = st.build(_deps(s), uuid4(), "0" * 64, st.datetime.now(st.UTC))
    assert msg["To"] == "ops@vektra.example.org" and "studenti" in msg.get_content()


async def test_an_already_attempted_row_is_never_sent_again() -> None:
    s = _Sender(SendResult(True, None, 250))
    assert await st.run_test(_deps(s), _ctx([], claim=False)) == st.INTERRUPTED
    assert s.sent == []
