"""``teacher_notify`` / ``operator_notify`` end to end: real Postgres, real producers where they
can run, the in-process TLS mail sink as the only evidence of a send (REQ-071, REQ-086).

Every scenario starts from a job the producer itself queued (the close function, the
Moodle-unreachable alert) or one queued with the producer's exact argument shape (the rollback
cap, whose streak is covered by the start-guard tests), and runs the bound handler on it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fixtures.mailsink import MailSink, sink_config
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import TEACHERS, config_pair, configure_recipients
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.delivery import notify as n
from vektra_assess.delivery.backoff import parse_schedule
from vektra_assess.delivery.recipients import ConfigRecipients
from vektra_assess.delivery.smtp import SmtpSender
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.worker.start_guard import alert_moodle_unreachable

pytestmark = pytest.mark.integration

OPS = "ops@vektra.example.org"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    async def sync(self, pg) -> Clock:  # type: ignore[no-untyped-def]
        self.now = (await pg.run("super", "SELECT now()"))[0][0] + timedelta(seconds=1)
        return self

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[MailSink]:
    s = MailSink(tmp_path / "sink").start()
    yield s
    s.stop()


def _deps(config, sink, clock, *, domains=("uni.example",), operator=OPS, sender=None):  # type: ignore[no-untyped-def]
    return n.NotifyDeps(
        recipients=config,
        sender=sender or SmtpSender(sink_config(sink)),
        sender_address="vektra@vektra.example.org",
        message_id_domain="vektra.example.org",
        domains=frozenset(domains),
        operator_address=operator,
        schedule=parse_schedule("1m,5m,15m,1h"),
        window=timedelta(hours=72),
        clock=clock,
    )


async def jobs(pg, kind: str) -> list[Any]:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT id, args, state, not_before, dedupe_key FROM assess_jobs WHERE kind = :k "
        "ORDER BY id",
        {"k": kind},
    )


def ctx(job, worker) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    args = job.args if isinstance(job.args, dict) else json.loads(job.args)
    return SimpleNamespace(
        job_id=int(job.id), args=args, attempt=1, acquire=worker.connect
    )


async def rows(pg, job_id: int) -> list[Any]:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT id, kind, recipient_ref, status, attempts, last_error_class, message_id, "
        "attempt_started_at, transcript_id FROM assess_deliveries WHERE job_id = :j "
        "ORDER BY recipient_ref",
        {"j": job_id},
    )


async def events(pg, event: str, job_id: int) -> list[Any]:  # type: ignore[no-untyped-def]
    """The audit rows of one notification (audit rows outlive the per-test reset)."""
    return await pg.run(
        "super",
        "SELECT reason_code, data, site_id, session_id FROM assess_audit_events "
        "WHERE event = :e AND (data->>'job')::bigint = :j ORDER BY id",
        {"e": event, "j": job_id},
    )


async def assert_no_address_stored(pg, addresses) -> None:  # type: ignore[no-untyped-def]
    dump = json.dumps(
        [
            [str(c) for c in row]
            for table in ("assess_deliveries", "assess_audit_events", "assess_jobs")
            for row in await pg.run("super", f"SELECT * FROM {table}")
        ]
    )
    for a in addresses:
        assert a not in dump


async def _service_failure(w, worker, addresses=TEACHERS, pair=None):  # type: ignore[no-untyped-def]
    """An ACTIVE session closed ``service_failure`` by the real close function, which queues
    the ``teacher_notify`` job in the close transaction."""
    s = await seed_session(w, worker, "ACTIVE")
    config = await configure_recipients(
        w.pg, w.site, s.cmid, addresses, pair=pair or config_pair()
    )
    epoch = (
        await w.pg.run(
            "super",
            "SELECT epoch FROM assess_sessions WHERE id = :s",
            {"s": s.session_id},
        )
    )[0][0]
    assert (
        await w.pg.run(
            "worker",
            "SELECT assess_close_session(:s, :e, 'service_failure')",
            {"s": s.session_id, "e": epoch},
        )
    )[0][0]
    (job,) = await jobs(w.pg, "teacher_notify")
    return s, config, job


async def test_a_service_failure_close_mails_every_configured_teacher_once(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        s, config, job = await _service_failure(w, worker)
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        got = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        assert got is not None and got.sent == 2 and not got.next_run
        stored = await rows(pg, int(job.id))
        assert {r.status for r in stored} == {"SENT"} and len(stored) == 2
        assert {r.kind for r in stored} == {"teacher_notify"}
        assert all(r.transcript_id is None for r in stored)
        number = (
            await pg.run(
                "super",
                "SELECT student_userid, session_no FROM assess_sessions WHERE id = :s",
                {"s": s.session_id},
            )
        )[0]
        for address in TEACHERS:
            (copy,) = sink.to(address)
            assert copy.tls and copy.authenticated
            msg = copy.message
            assert msg["Auto-Submitted"] == "auto-generated"
            body = msg.get_content()
            assert f"attivita': {s.cmid}" in body
            assert f"studente (id Moodle): {number.student_userid}" in body
            assert f"sessione: #{number.session_no}" in body
            assert "guasto del servizio" in str(msg["Subject"])
            assert not list(msg.iter_attachments())  # a notice, never the transcript
        for r in stored:
            assert sink.by_message_id(r.message_id)
        sent = await events(pg, "assess_notify_sent", int(job.id))
        assert len(sent) == 2
        assert {e.session_id for e in sent} == {s.session_id}
        assert {e.data["event"] for e in sent} == {"service_failure"}
        # the same job again (a retried job): nothing is sent twice
        again = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        assert again is not None and again.sent == 0
        assert len(sink.messages) == 2
        await assert_no_address_stored(pg, TEACHERS)


async def test_the_rollback_cap_tells_the_teacher_and_the_operator(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """REQ-071: both jobs, the producer's argument shape; the operator's email names no
    student."""
    clock = Clock()
    async with world(pg) as w:
        s = await seed_session(w, worker, "ELIGIBLE")
        config = await configure_recipients(w.pg, w.site, s.cmid, TEACHERS[:1])
        engine = create_async_engine(pg.url("worker"))
        args = {
            "site_id": str(w.site),
            "cmid": s.cmid,
            "student_userid": 4242,
            "reason": "rollback_cap",
        }
        async with engine.connect() as conn:
            for kind in (JobKind.TEACHER_NOTIFY, JobKind.OPERATOR_NOTIFY):
                await enqueue_job(conn, kind, args)
            await conn.commit()
        await engine.dispose()
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        (tj,) = await jobs(pg, "teacher_notify")
        (oj,) = await jobs(pg, "operator_notify")
        assert (await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(tj, worker))).sent == 1  # type: ignore[union-attr]
        assert (
            await n.notify(deps, JobKind.OPERATOR_NOTIFY, ctx(oj, worker))
        ).sent == 1  # type: ignore[union-attr]
        (teacher,) = sink.to(TEACHERS[0])
        assert "4242" in teacher.message.get_content()
        (ops,) = sink.to(OPS)
        body = ops.message.get_content()
        assert str(w.site) in body and f"attivita': {s.cmid}" in body
        assert "4242" not in body and "4242" not in str(ops.message["Subject"])
        await assert_no_address_stored(pg, (*TEACHERS, OPS))


async def test_an_unreadable_lms_alert_reaches_the_operator(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        engine = create_async_engine(pg.url("worker"))
        async with engine.connect() as conn:
            await alert_moodle_unreachable(
                conn, w.site, "capture_failed_attempt_unreadable"
            )
            await conn.commit()
        await engine.dispose()
        await clock.sync(pg)
        (job,) = await jobs(pg, "operator_notify")
        deps = _deps(ConfigRecipients(config_pair()[1]), sink, clock)
        got = await n.notify(deps, JobKind.OPERATOR_NOTIFY, ctx(job, worker))
        assert got is not None and got.sent == 1
        (ops,) = sink.to(OPS)
        body = ops.message.get_content()
        assert "capture_failed_attempt_unreadable" in body and str(w.site) in body
        # the teacher job never takes this event: it has no activity to point to
        assert await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker)) is None


async def test_a_crash_after_the_relay_accepted_resends_with_the_same_message_id(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """OQ-029: the relay stored the message, the connection dropped before the 250: the next
    run of the job sends again with the SAME Message-ID and audits the possible duplicate."""
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, TEACHERS[:1])
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        sink.fault = "drop_after_data"
        first = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        assert first is not None and first.retrying == 1
        # a crash in the window: the attempt start is committed, no outcome recorded
        await pg.run(
            "super",
            "UPDATE assess_deliveries SET attempt_started_at = now(), status = 'PENDING' "
            "WHERE job_id = :j",
            {"j": int(job.id)},
        )
        sink.fault = None
        clock.advance(
            minutes=5
        )  # past the lease: the marker is a crash, not a live send
        second = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        assert second is not None and second.sent == 1 and second.duplicates == 1
        (row,) = await rows(pg, int(job.id))
        copies = sink.by_message_id(row.message_id)
        assert len(copies) == 2  # the dropped copy and the resend: one id, recognisable
        assert (
            len(await events(pg, "assess_notify_possible_duplicate", int(job.id))) == 1
        )


async def test_a_relay_outage_queues_the_next_run_then_fails_past_the_window(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, TEACHERS[:1])
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        sink.down()
        got = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        assert got is not None and got.retrying == 1 and got.next_run is not None
        assert got.next_run == clock.now + timedelta(minutes=1)
        (row,) = await rows(pg, int(job.id))
        assert row.status == "RETRYING" and row.last_error_class == "connect"
        # the next run of the chain: same args, its root, try 2, the first run's time
        chain = [j for j in await jobs(pg, "teacher_notify") if j.id != job.id]
        assert len(chain) == 1
        nxt = chain[0]
        a = nxt.args if isinstance(nxt.args, dict) else json.loads(nxt.args)
        assert a["root_job_id"] == int(job.id) and a["try"] == 2
        assert a["session_id"] == ctx(job, worker).args["session_id"]
        # the relay is back: the chained run delivers on the SAME row
        sink.up()
        clock.advance(minutes=1)
        later = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(nxt, worker))
        assert later is not None and later.sent == 1
        assert [r.status for r in await rows(pg, int(job.id))] == ["SENT"]
        assert (
            await rows(pg, int(nxt.id)) == []
        )  # keyed by the chain's root, not the run


async def test_past_the_window_the_rows_fail_with_an_alert(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, TEACHERS[:1])
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        sink.down()
        await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        nxt = next(j for j in await jobs(pg, "teacher_notify") if j.id != job.id)
        clock.advance(hours=73)
        got = await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(nxt, worker))
        assert got is not None and got.next_run is None and got.failed == 1
        (row,) = await rows(pg, int(job.id))
        assert row.status == "FAILED" and row.last_error_class == "window_expired"
        failed = await events(pg, "assess_notify_failed", int(job.id))
        assert [e.reason_code for e in failed] == ["window_expired"]
        assert any(
            '"alert": "notify_failed"' in line for line in canary_log_capture.lines()
        )


async def test_an_address_outside_the_allowlist_is_never_mailed(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, ("prof@elsewhere.example",))
        await clock.sync(pg)
        got = await n.notify(
            _deps(config, sink, clock), JobKind.TEACHER_NOTIFY, ctx(job, worker)
        )
        assert got is not None and got.failed == 1 and got.sent == 0
        (row,) = await rows(pg, int(job.id))
        assert row.status == "FAILED" and row.last_error_class == "domain_not_allowed"
        assert sink.messages == []


async def test_nobody_configured_is_a_failed_notification_not_a_silent_one(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, ())
        await clock.sync(pg)
        got = await n.notify(
            _deps(config, sink, clock), JobKind.TEACHER_NOTIFY, ctx(job, worker)
        )
        assert got is not None and got.failed == 1 and got.next_run is None
        assert [
            e.reason_code for e in await events(pg, "assess_notify_failed", int(job.id))
        ] == ["no_recipients"]
        assert any(
            '"alert": "notify_failed"' in line for line in canary_log_capture.lines()
        )
        assert sink.messages == []


async def test_unreadable_recipients_wait_for_the_next_run(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, _, job = await _service_failure(w, worker, TEACHERS[:1])
        await clock.sync(pg)
        wrong = ConfigRecipients(
            config_pair("w9")[1]
        )  # a key that does not open the seal
        got = await n.notify(
            _deps(wrong, sink, clock), JobKind.TEACHER_NOTIFY, ctx(job, worker)
        )
        assert got is not None and got.unresolved and got.next_run is not None
        assert await rows(pg, int(job.id)) == [] and sink.messages == []


async def test_a_purged_session_makes_the_job_a_no_op(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, TEACHERS[:1])
        args = dict(
            ctx(job, worker).args, session_id="00000000-0000-0000-0000-000000000001"
        )
        stale = SimpleNamespace(
            job_id=int(job.id), args=args, attempt=1, acquire=worker.connect
        )
        assert (
            await n.notify(_deps(config, sink, clock), JobKind.TEACHER_NOTIFY, stale)
            is None
        )
        assert sink.messages == []


async def test_a_crash_marker_that_outlives_the_window_fails_as_unknown(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """Review #5: the window ends on a row whose last attempt left no outcome: the failure
    records that the relay may hold the mail (the marker read BEFORE it is cleared)."""
    clock = Clock()
    async with world(pg) as w:
        _, config, job = await _service_failure(w, worker, TEACHERS[:1])
        await clock.sync(pg)
        deps = _deps(config, sink, clock)
        sink.down()
        await n.notify(deps, JobKind.TEACHER_NOTIFY, ctx(job, worker))
        await pg.run(
            "super",
            "UPDATE assess_deliveries SET attempt_started_at = now() WHERE job_id = :j",
            {"j": int(job.id)},
        )
        nxt = next(j for j in await jobs(pg, "teacher_notify") if j.id != job.id)
        clock.advance(hours=73)
        # the recipients cannot be read on the last run: no new attempt replaces the marker
        unreadable = _deps(ConfigRecipients(config_pair("w9")[1]), sink, clock)
        got = await n.notify(unreadable, JobKind.TEACHER_NOTIFY, ctx(nxt, worker))
        assert got is not None and got.next_run is None
        (failed,) = await events(pg, "assess_notify_failed", int(job.id))
        assert failed.reason_code == "window_expired"
        assert failed.data["last_attempt_outcome"] == "unknown"
