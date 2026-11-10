"""The operator's SMTP test send end to end (plan assess-privacy-retention 12b, REQ-080).

Real Postgres and the real routes; the in-process TLS mail sink is the only evidence of a send.
The recipient is the configured operator address whatever the request says.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures.mailsink import MailSink, sink_config
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import auth, default_settings, err, world

from integration.test_session_preflight import _reset
from vektra_assess.delivery import smtp_test as st
from vektra_assess.delivery.smtp import SmtpSender
from vektra_assess.privacy.purge import run_purge

pytestmark = pytest.mark.integration

OPS = "ops@vektra.example.org"
RELAY = {
    "VEKTRA_ASSESS_SMTP_HOST": "relay.vektra.example.org",
    "VEKTRA_ASSESS_SMTP_FROM": "vektra@vektra.example.org",
    "VEKTRA_ASSESS_OPERATOR_EMAIL": OPS,
}


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


def _deps(sink: MailSink, sender=None) -> st.SmtpTestDeps:  # type: ignore[no-untyped-def]
    return st.SmtpTestDeps(
        sender=sender or SmtpSender(sink_config(sink)),
        sender_address="vektra@vektra.example.org",
        message_id_domain="vektra.example.org",
        operator_address=OPS,
    )


async def _job(pg):  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT id, args, max_attempts FROM assess_jobs WHERE kind = 'smtp_test_send' "
        "ORDER BY id DESC LIMIT 1",
    )
    return rows[0]


def _ctx(job, worker) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    args = job.args if isinstance(job.args, dict) else json.loads(job.args)
    return SimpleNamespace(
        job_id=int(job.id), args=args, attempt=1, acquire=worker.connect
    )


async def test_the_operator_queues_a_test_and_reads_its_outcome(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(**RELAY)) as w:
        url = "/api/v1/assess/ops/smtp-test"
        # an address in the request is refused: the recipient is never the caller's
        bad = await w.client.post(
            url, json={"to": "x@elsewhere.example"}, headers=auth("admin")
        )
        assert bad.status_code == 400 and "elsewhere" not in bad.text
        for key in ("teacher", "mint"):
            assert (await w.client.post(url, headers=auth(key))).status_code == 403, key

        r = await w.client.post(url, headers=auth("admin"))
        assert r.status_code == 202, r.text
        test_id = r.json()["id"]
        again = await w.client.post(url, headers=auth("admin"))
        assert (
            again.status_code == 429
            and err(again)["details"]["reason"] == "smtp_test_pending"
        )
        assert (await w.client.get(f"{url}/{test_id}", headers=auth("admin"))).json()[
            "status"
        ] == "queued"

        job = await _job(pg)
        assert job.max_attempts == 1
        assert await st.run_test(_deps(sink), _ctx(job, worker)) == "sent"
        (msg,) = sink.to(OPS)
        assert msg.message["Subject"] == st.SUBJECT
        got = (await w.client.get(f"{url}/{test_id}", headers=auth("admin"))).json()
        assert got["status"] == "SENT" and got["error_class"] is None and got["sent_at"]
        assert OPS not in json.dumps(got)

        # a rerun of the same job (lease expired after a crash) never sends a second copy
        assert await st.run_test(_deps(sink), _ctx(job, worker)) == "interrupted"
        assert len(sink.to(OPS)) == 1
        audited = await pg.run(
            "super",
            "SELECT event, reason_code FROM assess_audit_events WHERE data->>'test_id' = :t "
            "ORDER BY id",
            {"t": test_id},
        )
        assert [tuple(a) for a in audited] == [
            ("assess_smtp_test_requested", None),
            ("assess_smtp_test_result", "sent"),
        ]
        dump = json.dumps([[str(c) for c in row] for row in await pg.run(
            "super", "SELECT * FROM assess_deliveries WHERE id = :i", {"i": test_id})])  # fmt: skip
        assert OPS not in dump


async def test_a_relay_failure_is_reported_by_class(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(**RELAY)) as w:
        r = await w.client.post("/api/v1/assess/ops/smtp-test", headers=auth("admin"))
        test_id = r.json()["id"]
        sink.down()
        assert await st.run_test(_deps(sink), _ctx(await _job(pg), worker)) == "connect"
        got = (
            await w.client.get(
                f"/api/v1/assess/ops/smtp-test/{test_id}", headers=auth("admin")
            )
        ).json()
        assert (got["status"], got["error_class"]) == ("FAILED", "connect")
        # review #6: a test send never reaches the production delivery figures
        snap = (await pg.run("api", "SELECT public.assess_metrics_snapshot()"))[0][0]
        prod = (
            await pg.run(
                "super",
                "SELECT count(*) FROM assess_deliveries WHERE status = 'FAILED' "
                "AND kind <> 'smtp_test'",
            )
        )[0][0]
        assert snap["deliveries"].get("FAILED", 0) == prod


async def test_without_the_relay_the_test_is_refused(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        r = await w.client.post("/api/v1/assess/ops/smtp-test", headers=auth("admin"))
        assert r.status_code == 503 and "smtp_relay" in err(r)["remediation"]
        bad = await w.client.get(
            "/api/v1/assess/ops/smtp-test/not-a-uuid", headers=auth("admin")
        )
        assert bad.status_code == 400


async def test_the_purge_drops_week_old_test_rows(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(**RELAY)) as w:
        r = await w.client.post("/api/v1/assess/ops/smtp-test", headers=auth("admin"))
        test_id = r.json()["id"]
        await st.run_test(_deps(sink), _ctx(await _job(pg), worker))
        await pg.run(
            "super",
            "UPDATE assess_deliveries SET created_at = now() - interval '8 days' WHERE id = :i",
            {"i": test_id},
        )
        report = await run_purge(worker.connect)
        assert report.smtp_tests == 1
        left = await pg.run(
            "super",
            "SELECT count(*) FROM assess_deliveries WHERE id = :i",
            {"i": test_id},
        )
        assert left[0][0] == 0


async def test_a_test_whose_job_died_is_reaped_to_interrupted(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review #2: the job has one attempt, so a test killed mid-send (or before it ran) had no
    rerun to settle it and read PENDING for a week. The tick reaps it."""
    from datetime import timedelta

    async with world(pg, default_settings(**RELAY)) as w:
        r = await w.client.post("/api/v1/assess/ops/smtp-test", headers=auth("admin"))
        test_id = r.json()["id"]
        job = await _job(pg)
        # the run claimed the row and the worker was killed: the job is dead, the row PENDING
        await pg.run(
            "super",
            "INSERT INTO assess_deliveries (id, kind, recipient_kind, recipient_ref, status, "
            "attempts, attempt_started_at) VALUES (:i, 'smtp_test', 'address', :ref, "
            "'PENDING', 1, now())",
            {"i": test_id, "ref": "0" * 64},
        )
        later = (await pg.run("super", "SELECT now() + interval '10 min'"))[0][0]
        # while the job is still ready the row is left alone
        assert (
            await st.reap_interrupted_tests(
                worker.connect, stale_after=timedelta(minutes=4), now=later
            )
            == 0
        )
        await pg.run("super", "UPDATE assess_jobs SET state = 'dead' WHERE id = :j",
                     {"j": job.id})  # fmt: skip
        assert (
            await st.reap_interrupted_tests(
                worker.connect, stale_after=timedelta(minutes=4), now=later
            )
            == 1
        )
        got = (
            await w.client.get(
                f"/api/v1/assess/ops/smtp-test/{test_id}", headers=auth("admin")
            )
        ).json()
        assert (got["status"], got["error_class"]) == ("FAILED", "interrupted")
