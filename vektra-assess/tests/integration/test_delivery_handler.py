"""Plan 10b: ``transcript_deliver`` end to end, real Postgres + the in-process TLS mail sink.

The sink is the only accepted evidence of a send: every SENT row is matched to a stored message by
its Message-ID (``assert_sent_rows_are_on_the_sink`` runs after every scenario), and a refusal is
checked to have left nothing there. Time-dependent behaviour (a 2 h outage, the 72 h window, the
crash window) runs on an injected clock; the relay is really down, really refusing, really
without STARTTLS.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fixtures.mailsink import MailSink, sink_config
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import TEACHERS, config_pair, configure_recipients, refs
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from integration.test_transcript_api import download, mint
from vektra_assess.delivery import handler as h
from vektra_assess.delivery.backoff import parse_schedule
from vektra_assess.delivery.recipients import recipient_ref
from vektra_assess.delivery.smtp import SmtpSender
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

pytestmark = pytest.mark.integration

STALE = timedelta(seconds=240)


class Clock:
    """The handler's clock, started from the DATABASE's clock (rows get ``now()`` defaults;
    a container VM may run a little apart from the host)."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    async def sync(self, pg) -> Clock:  # type: ignore[no-untyped-def]
        db_now = (await pg.run("super", "SELECT now()"))[0][0]
        self.now = db_now + timedelta(seconds=1)
        return self

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Facts:
    def __init__(self, course: str = "EA-2026") -> None:
        self.course = course

    async def close_facts(self, **_: Any) -> CloseFacts:
        return CloseFacts(course_shortname=self.course)


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


async def _ready(w, worker, addresses=TEACHERS, *, pair=None, course="EA-2026"):  # type: ignore[no-untyped-def]
    """A CLOSED session, its configured recipients and its generated transcript."""
    ring = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
    w.app.state.assess_keys[store.KEYRING] = ring
    s = await seed_session(w, worker, "CLOSED")
    pair = pair or config_pair()
    config = await configure_recipients(w.pg, w.site, s.cmid, addresses, pair=pair)
    got = await generate_transcript(
        GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(course), recipients=config
        ),
        worker.connect,
        s.session_id,
    )
    assert got is not None and got.created
    return s, ring, config, pair


def _deps(ring, config, sink, clock, *, domains=("unical.it",), sender=None):  # type: ignore[no-untyped-def]
    return h.DeliveryDeps(
        transcript_kek=ring,
        recipients=config,
        sender=sender or SmtpSender(sink_config(sink)),
        sender_address="vektra@vektra.example.org",
        message_id_domain="vektra.example.org",
        domains=frozenset(domains),
        schedule=parse_schedule("1m,5m,15m,1h"),
        window=timedelta(hours=72),
        stale_after=STALE,
        clock=clock,
    )


async def rows(pg, tid) -> list[Any]:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT id, round, recipient_ref, status, attempts, not_before, last_error_class, "
        "smtp_code, message_id, sent_at, attempt_started_at FROM assess_deliveries "
        "WHERE transcript_id = :t ORDER BY round, recipient_ref",
        {"t": tid},
    )


async def events(pg, tid, event: str) -> list[Any]:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT reason_code, data FROM assess_audit_events WHERE session_id = :s "
        "AND event = :e ORDER BY id",
        {"s": tid, "e": event},
    )


async def assert_sent_rows_are_on_the_sink(pg, tid, sink: MailSink) -> None:  # type: ignore[no-untyped-def]
    """REQ-075: no row is SENT without a sink-recorded message of the same Message-ID."""
    for r in await rows(pg, tid):
        if r.status == "SENT":
            assert r.message_id and sink.by_message_id(r.message_id), r


async def assert_no_address_stored(pg, addresses) -> None:  # type: ignore[no-untyped-def]
    dump = json.dumps(
        [
            [str(c) for c in row]
            for table in ("assess_deliveries", "assess_audit_events", "assess_jobs")
            for row in await pg.run("super", f"SELECT * FROM {table}")
        ]
    )
    for a in addresses:
        local = a.split("@")[0]
        assert a not in dump and (len(local) < 4 or local not in dump)


def alerts(cap) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    out = []
    for line in cap.lines():
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if doc.get("event") == "assess.delivery.alert":
            out.append(doc)
    return out


async def test_each_recipient_gets_the_downloaded_bytes_once(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    """REQ-074 / REQ-075: one message per recipient, over TLS with AUTH; the attachment
    hash-equals the stored hash AND the bytes the student downloads."""
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        got = await h.deliver_transcript(
            _deps(ring, config, sink, clock), worker.connect, s.session_id
        )
        assert got is not None and got.sent == 2
        sent = await rows(pg, s.session_id)
        assert {r.status for r in sent} == {"SENT"} and len(sent) == 2
        token = (await mint(w, s.session_id, sub=s.sub)).json()["token"]
        downloaded = (await download(w, s.session_id, token)).content
        stored = (
            await pg.run(
                "super",
                "SELECT content_hash FROM assess_transcripts WHERE id = :t",
                {"t": s.session_id},
            )
        )[0][0]
        for address in TEACHERS:
            copies = sink.to(address)
            assert len(copies) == 1 and copies[0].tls and copies[0].authenticated
            att = next(copies[0].message.iter_attachments()).get_payload(decode=True)
            assert att == downloaded
            assert hashlib.sha256(att).hexdigest() == stored
        assert len(sink.messages) == 2
        # a second job finds nothing due: still two messages
        again = await h.deliver_transcript(
            _deps(ring, config, sink, clock), worker.connect, s.session_id
        )
        assert again is not None and again.sent == 0 and len(sink.messages) == 2
        assert len(await events(pg, s.session_id, "assess_delivery_sent")) == 2
        attempts = await events(pg, s.session_id, "assess_delivery_attempt")
        assert [a.data["outcome"] for a in attempts] == ["sent", "sent"]
        assert {a.data["ref"] for a in attempts} == refs(w.site)
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)
        await assert_no_address_stored(pg, TEACHERS)
    canary_log_capture.assert_clean(TEACHERS, require_lines=True)


async def test_smtp_down_two_hours_then_up_delivers_exactly_once(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """REQ-075: the relay is unreachable for a simulated 2 h; every attempt is a real refused
    connection; once it is back, each recipient holds exactly one message."""
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        t0 = clock.now
        deps = _deps(ring, config, sink, clock)
        sink.down()
        runs = 0
        while clock.now < t0 + timedelta(hours=2):
            got = await h.deliver_transcript(deps, worker.connect, s.session_id)
            assert got is not None and got.sent == 0
            runs += 1
            current = await rows(pg, s.session_id)
            assert {r.status for r in current} == {"RETRYING"}
            assert {r.last_error_class for r in current} == {"connect"}
            # the tick sees the due rows when their time comes, and queues one job
            clock.now = min(r.not_before for r in current)
        assert runs >= 5  # 1, 5, 15 min, then hourly: the backoff really ran
        tick = await h.delivery_retry_tick(
            worker.connect, stale_after=STALE, window=deps.window, now=clock.now
        )
        assert tick["queued"] == 1
        sink.up()
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 2
        for address in TEACHERS:
            assert len(sink.to(address)) == 1
        assert len(sink.messages) == 2
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)


async def test_a_relay_without_starttls_gets_nothing_and_alerts(
    pg, worker, tmp_path, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    plain = MailSink(tmp_path / "plain", starttls=False, require_auth=False).start()
    try:
        clock = Clock()
        async with world(pg) as w:
            s, ring, config, _ = await _ready(w, worker)
            await clock.sync(pg)
            got = await h.deliver_transcript(
                _deps(ring, config, plain, clock), worker.connect, s.session_id
            )
            assert got is not None and got.retrying == 2 and got.sent == 0
            current = await rows(pg, s.session_id)
            assert {(r.status, r.last_error_class) for r in current} == {
                ("RETRYING", "tls")
            }
            assert plain.messages == [] and plain.auth_attempts == []
        assert [a["alert"] for a in alerts(canary_log_capture)].count("smtp_tls") == 2
    finally:
        plain.stop()


async def test_a_crash_between_250_and_the_sent_commit_is_one_audited_duplicate(
    pg, worker, sink, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """OQ-029: the relay accepted, the worker died before committing SENT. After the lease the
    row is resent with the SAME Message-ID: one SENT row per recipient, at most one duplicate
    on the sink, and an ``assess_delivery_possible_duplicate`` event per resent row."""
    clock = Clock()
    real_settle = h._settle

    async def crash_after_relay(*args: Any, **kw: Any) -> None:
        if args[4].ok:  # the relay said 250
            raise RuntimeError("worker killed before the outcome commit")
        await real_settle(*args, **kw)

    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        monkeypatch.setattr(h, "_settle", crash_after_relay)
        with pytest.raises(RuntimeError):
            await h.deliver_transcript(deps, worker.connect, s.session_id)
        monkeypatch.setattr(h, "_settle", real_settle)
        assert len(sink.messages) == 2
        stuck = await rows(pg, s.session_id)
        assert {r.status for r in stuck} == {"PENDING"}
        assert all(r.attempt_started_at is not None for r in stuck)
        # inside the lease: the attempt may still be running, nothing is taken again
        clock.advance(seconds=10)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 0 and len(sink.messages) == 2
        clock.now += STALE
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 2 and got.duplicates == 2
        final = await rows(pg, s.session_id)
        assert [r.status for r in final] == ["SENT", "SENT"]
        for r in final:
            copies = sink.by_message_id(r.message_id)
            assert len(copies) == 2  # the original and at most one known duplicate
        dup = await events(pg, s.session_id, "assess_delivery_possible_duplicate")
        assert {d.data["ref"] for d in dup} == refs(w.site) and len(dup) == 2
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)


async def test_the_72_hour_window_ends_in_failed_with_an_alert(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.down()
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        clock.advance(hours=72, minutes=1)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.failed == 2
        final = await rows(pg, s.session_id)
        assert {(r.status, r.last_error_class) for r in final} == {
            ("FAILED", "connect")
        }
        assert len(await events(pg, s.session_id, "assess_delivery_failed")) == 2
        # FAILED is final: the relay coming back changes nothing
        sink.up()
        clock.advance(hours=1)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 0 and sink.messages == []
    assert [a["alert"] for a in alerts(canary_log_capture)].count(
        "delivery_failed"
    ) == 2


async def test_a_header_injection_in_the_course_is_one_header_on_the_wire(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        # the stored course short name (from Moodle) and the attachment's file name, as a
        # hostile course name or an attacker with DB write access would set them
        await pg.run(
            "super",
            "UPDATE assess_transcripts SET course_shortname = :c, filename = :f WHERE id = :t",
            {
                "c": "EA\r\nBcc: victim@evil.example\r\nX-Evil: 1",
                "f": "EA\r\nBcc: victim@evil.example\r\nX-Evil: 1_9_9_s1_20261107.html",
                "t": s.session_id,
            },
        )
        got = await h.deliver_transcript(
            _deps(ring, config, sink, clock), worker.connect, s.session_id
        )
        assert got is not None and got.sent == 2
        for item in sink.messages:
            head = item.raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
            assert sum(1 for ln in head if ln.lower().startswith(b"subject:")) == 1
            assert not any(ln.lower().startswith((b"bcc:", b"x-evil:")) for ln in head)
            assert "victim@evil.example" not in item.rcpt
        assert sink.to("victim@evil.example") == []


async def test_a_removed_address_is_replaced_and_a_fourth_never_exists(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """REQ-082 as amended: re-read at send time. Removed -> REPLACED (old ref audited), added
    -> its own row (new ref audited), never more than 3 rows per round that are not REPLACED."""
    a, b, c, d, e, f = (f"{x}@unical.it" for x in "abcdef")
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, pair = await _ready(w, worker, (a, b, c))
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.fault = "4xx"
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert {r.status for r in await rows(pg, s.session_id)} == {"RETRYING"}
        # the teacher edits the activity: b and c out, d, e, f in
        await configure_recipients(pg, w.site, s.cmid, (a, d, e, f), pair=pair)
        sink.fault = None
        clock.advance(minutes=2)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and (got.replaced, got.added, got.sent) == (2, 2, 3)
        final = {r.recipient_ref: r.status for r in await rows(pg, s.session_id)}
        ref = {x: recipient_ref(w.site, x) for x in (a, b, c, d, e, f)}
        assert final == {
            ref[a]: "SENT",
            ref[b]: "REPLACED",
            ref[c]: "REPLACED",
            ref[d]: "SENT",
            ref[e]: "SENT",
        }  # f: no 4th recipient in the round
        assert [len(sink.to(x)) for x in (a, b, c, d, e, f)] == [1, 0, 0, 1, 1, 0]
        old = await events(pg, s.session_id, "assess_delivery_recipient_replaced")
        new = await events(pg, s.session_id, "assess_delivery_recipient_added")
        assert {o.data["old"] for o in old} == {ref[b], ref[c]}
        assert {n.data["new"] for n in new} == {ref[d], ref[e]}
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)
        await assert_no_address_stored(pg, (a, b, c, d, e, f))


async def test_an_address_outside_the_domain_allowlist_gets_nothing(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    inside, outside = "prof@unical.it", "prof@gmail.example"
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker, (inside, outside))
        await clock.sync(pg)
        got = await h.deliver_transcript(
            _deps(ring, config, sink, clock), worker.connect, s.session_id
        )
        assert got is not None and (got.sent, got.failed) == (1, 1)
        by_ref = {r.recipient_ref: r for r in await rows(pg, s.session_id)}
        bad = by_ref[recipient_ref(w.site, outside)]
        assert (bad.status, bad.last_error_class) == ("FAILED", "domain_not_allowed")
        assert sink.to(outside) == [] and len(sink.to(inside)) == 1
    async with world(pg) as w:
        # an empty allowlist refuses every address
        s, ring, config, _ = await _ready(w, worker, (inside,))
        await clock.sync(pg)
        sink.clear()
        got = await h.deliver_transcript(
            _deps(ring, config, sink, clock, domains=()), worker.connect, s.session_id
        )
        assert got is not None and got.failed == 1 and sink.messages == []
    fails = [a for a in alerts(canary_log_capture) if a["alert"] == "delivery_failed"]
    assert {a["reason_code"] for a in fails} == {"domain_not_allowed"} and len(
        fails
    ) == 2


async def test_synchronous_replies_retry_on_4xx_and_fail_on_5xx(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.fault = "4xx"
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert {(r.status, r.smtp_code) for r in await rows(pg, s.session_id)} == {
            ("RETRYING", 451)
        }
        sink.fault = "5xx"
        clock.advance(minutes=2)
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        final = await rows(pg, s.session_id)
        assert {(r.status, r.last_error_class, r.smtp_code) for r in final} == {
            ("FAILED", "perm_5xx", 550)
        }
        assert sink.messages == []


async def test_recipients_configured_after_the_close_are_fanned_out(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """No readable config at generation: no row. The tick queues a (throttled) fan-out job;
    once the teacher configured the activity, round 1 is created and delivered. A config that
    becomes unreadable keeps the rows waiting (RETRYING, recipients_unresolved)."""
    clock = Clock()
    async with world(pg) as w:
        ring = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
        s = await seed_session(w, worker, "CLOSED")
        await generate_transcript(
            GenerationDeps(kek=s.kek, transcript_kek=ring, facts=Facts()),
            worker.connect,
            s.session_id,
        )
        assert await rows(pg, s.session_id) == []
        await clock.sync(pg)
        pub, priv = config_pair()
        from vektra_assess.delivery.recipients import ConfigRecipients

        deps = _deps(ring, ConfigRecipients(priv), sink, clock)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.unresolved and await rows(pg, s.session_id) == []
        tick = await h.delivery_retry_tick(
            worker.connect, stale_after=STALE, window=deps.window, now=clock.now
        )
        assert tick["fanout"] == 1
        await configure_recipients(pg, w.site, s.cmid, pair=(pub, priv))
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and (got.added, got.sent) == (2, 2)
        assert {len(sink.to(a)) for a in TEACHERS} == {1}
        # a resend round while the config cannot be opened: rows wait, nothing is sent
        await pg.run(
            "super",
            "UPDATE assess_deliveries SET created_at = now() - interval '1 hour' "
            "WHERE transcript_id = :t",
            {"t": s.session_id},
        )
        await pg.run(
            "api",
            "SELECT assess_enqueue_resend(:t, 'operator', 0)",
            {"t": s.session_id},
        )
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET teacher_config_sealed = NULL, "
            "teacher_config_kid = NULL WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.unresolved and got.retrying == 2
        round2 = [r for r in await rows(pg, s.session_id) if r.round == 2]
        assert {(r.status, r.last_error_class) for r in round2} == {
            ("RETRYING", "recipients_unresolved")
        }
        assert len(sink.messages) == 2
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)


async def test_the_subject_carries_the_real_course_short_name(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    """Review #9: the short name read at close is stored with the transcript and used as is
    (sanitised for the header), not the folded file-name prefix."""
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker, course="EA 2026/27")
        await clock.sync(pg)
        got = await h.deliver_transcript(
            _deps(ring, config, sink, clock), worker.connect, s.session_id
        )
        assert got is not None and got.sent == 2
        for item in sink.messages:
            subject = str(item.message["Subject"])
            assert "EA 2026/27" in subject and "EA-2026-27" not in subject


async def test_a_recipient_removed_and_added_back_is_mailed(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    """Review #2: removed -> REPLACED; configured again in the same round -> the same row goes
    back to PENDING (audited as re-added) and is delivered."""
    a, b = "a@unical.it", "b@unical.it"
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, pair = await _ready(w, worker, (a, b))
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.fault = "4xx"
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        await configure_recipients(pg, w.site, s.cmid, (a,), pair=pair)
        clock.advance(minutes=2)
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        status = {r.recipient_ref: r.status for r in await rows(pg, s.session_id)}
        assert status[recipient_ref(w.site, b)] == "REPLACED"
        await configure_recipients(pg, w.site, s.cmid, (a, b), pair=pair)
        sink.fault = None
        clock.advance(minutes=10)
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        final = await rows(pg, s.session_id)
        assert {r.status for r in final} == {"SENT"} and len(final) == 2
        assert len(sink.to(a)) == 1 and len(sink.to(b)) == 1
        readded = await events(pg, s.session_id, "assess_delivery_recipient_added")
        assert [(e.reason_code, e.data["new"]) for e in readded] == [
            ("re_added", recipient_ref(w.site, b))
        ]
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)


async def test_a_failed_address_no_longer_configured_frees_its_slot(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """Review #3: the cap counts the refs configured NOW. A FAILED row of an address the
    teacher removed stays as history and does not keep the corrected address out."""
    a, bad, b, c = "a@unical.it", "x@gmail.example", "b@unical.it", "c@unical.it"
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, pair = await _ready(w, worker, (a, bad, b))
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.fault = "4xx"
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        by = {r.recipient_ref: r.status for r in await rows(pg, s.session_id)}
        assert by[recipient_ref(w.site, bad)] == "FAILED"
        await configure_recipients(pg, w.site, s.cmid, (a, b, c), pair=pair)
        sink.fault = None
        clock.advance(minutes=2)
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        by = {r.recipient_ref: r.status for r in await rows(pg, s.session_id)}
        assert by == {
            recipient_ref(w.site, a): "SENT",
            recipient_ref(w.site, b): "SENT",
            recipient_ref(w.site, c): "SENT",
            recipient_ref(w.site, bad): "FAILED",  # history, not a slot
        }
        assert [len(sink.to(x)) for x in (a, b, c, bad)] == [1, 1, 1, 0]


async def test_a_job_with_nothing_due_never_loads_the_ciphertext(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """Review #8: the transcript bytes are read only after a claim returned rows."""
    import re

    from sqlalchemy import event

    reads: list[str] = []

    def spy(_c, _cur, statement, *_a) -> None:  # type: ignore[no-untyped-def]
        if re.search(r"ciphertext\s*,|ciphertext\s+FROM", statement, re.I):
            reads.append(statement)

    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        event.listen(worker.sync_engine, "before_cursor_execute", spy)
        try:
            got = await h.deliver_transcript(deps, worker.connect, s.session_id)
            assert got is not None and got.sent == 2 and reads  # bytes read to send
            reads.clear()
            got = await h.deliver_transcript(deps, worker.connect, s.session_id)
            assert got is not None and got.sent == 0
            assert reads == []  # nothing due: the ciphertext was never selected
        finally:
            event.remove(worker.sync_engine, "before_cursor_execute", spy)


async def test_no_free_relay_session_leaves_the_rows_unclaimed(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """Review #5: a row is claimed (attempt start committed) only once a relay session is
    held; waiting for one is bounded, and a row that could not get one stays untouched."""
    import asyncio

    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        sender = SmtpSender(sink_config(sink, max_connections=1))
        sender.slot_wait_s = 0.3
        deps = _deps(ring, config, sink, clock, sender=sender)
        held, release = asyncio.Event(), asyncio.Event()

        async def hog() -> None:
            async with sender.slot():
                held.set()
                await release.wait()

        task = asyncio.create_task(hog())
        await held.wait()
        try:
            async with asyncio.timeout(5):
                got = await h.deliver_transcript(deps, worker.connect, s.session_id)
            assert got is not None and got.sent == 0 and got.busy == 2
            current = await rows(pg, s.session_id)
            assert {(r.status, r.attempts, r.attempt_started_at) for r in current} == {
                ("PENDING", 0, None)
            }
            assert sink.messages == []
        finally:
            release.set()
            await task
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 2


async def test_a_row_past_its_window_is_failed_not_sent(
    pg, worker, sink, canary_log_capture
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4504367045: the worker was down past the 72 h window; when it comes back
    the rows are FAILED (window_expired, audited, alerted), never sent late."""
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.down()
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        sink.up()
        clock.advance(hours=80)  # well past created_at + 72 h + the lease grace
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 0 and got.failed == 2
        final = await rows(pg, s.session_id)
        assert {(r.status, r.last_error_class) for r in final} == {
            ("FAILED", "window_expired")
        }
        assert sink.messages == []
        failed = await events(pg, s.session_id, "assess_delivery_failed")
        assert [f.reason_code for f in failed] == ["window_expired"] * 2
    fails = [a for a in alerts(canary_log_capture) if a["alert"] == "delivery_failed"]
    assert {a["reason_code"] for a in fails} == {"window_expired"}


async def test_the_final_attempt_at_the_window_end_still_runs(pg, worker, sink) -> None:  # type: ignore[no-untyped-def]
    """The last attempt is scheduled exactly at the end of the window: a job that runs it a
    little late (tick, queue) still sends."""
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        sink.down()
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        sink.up()
        clock.advance(hours=72, seconds=60)  # inside the lease grace after the deadline
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 2
        await assert_sent_rows_are_on_the_sink(pg, s.session_id, sink)


async def test_an_unreadable_config_keeps_the_crash_marker(
    pg, worker, sink, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4504367050: a crash after the 250, then the config cannot be opened. The
    rows wait WITHOUT losing their attempt marker, so the resend, once the config is back, is
    still audited as a possible duplicate."""
    clock = Clock()
    real_settle = h._settle

    async def crash_after_relay(*args: Any, **kw: Any) -> None:
        if args[4].ok:
            raise RuntimeError("worker killed before the outcome commit")
        await real_settle(*args, **kw)

    async with world(pg) as w:
        s, ring, config, pair = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        monkeypatch.setattr(h, "_settle", crash_after_relay)
        with pytest.raises(RuntimeError):
            await h.deliver_transcript(deps, worker.connect, s.session_id)
        monkeypatch.setattr(h, "_settle", real_settle)
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET teacher_config_sealed = NULL, "
            "teacher_config_kid = NULL WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        clock.now += STALE + timedelta(seconds=1)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.unresolved
        waiting = await rows(pg, s.session_id)
        assert all(r.attempt_started_at is not None for r in waiting)
        # CodeRabbit 4504591109: deferred by the backoff, not offered again every tick
        assert all(r.not_before > clock.now for r in waiting)
        await h.delivery_retry_tick(
            worker.connect, stale_after=STALE, window=deps.window, now=clock.now
        )
        queued = await pg.run(
            "super",
            "SELECT count(*) FROM assess_jobs WHERE dedupe_key = :k",
            {"k": f"transcript_deliver:{s.session_id}:retry"},
        )
        assert (
            queued[0][0] == 0
        )  # the tick module-wide sees other tests' rows: count ours
        clock.now = max(r.not_before for r in waiting)
        await configure_recipients(pg, w.site, s.cmid, pair=pair)
        got = await h.deliver_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.sent == 2 and got.duplicates == 2
        dup = await events(pg, s.session_id, "assess_delivery_possible_duplicate")
        assert len(dup) == 2
        attempts = await events(pg, s.session_id, "assess_delivery_attempt")
        assert [
            a.data["duplicate"] for a in attempts if a.data["outcome"] == "sent"
        ] == [
            True,
            True,
        ]


async def test_the_crash_resend_keeps_the_stored_message_id(
    pg, worker, sink, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4504367056: the Message-ID is stored at the claim and reused, so a resend
    after a crash carries the same id even if VEKTRA_ASSESS_MESSAGE_ID_DOMAIN changed."""
    from dataclasses import replace

    clock = Clock()
    real_settle = h._settle

    async def crash_after_relay(*args: Any, **kw: Any) -> None:
        if args[4].ok:
            raise RuntimeError("worker killed before the outcome commit")
        await real_settle(*args, **kw)

    async with world(pg) as w:
        s, ring, config, _ = await _ready(w, worker)
        await clock.sync(pg)
        deps = _deps(ring, config, sink, clock)
        monkeypatch.setattr(h, "_settle", crash_after_relay)
        with pytest.raises(RuntimeError):
            await h.deliver_transcript(deps, worker.connect, s.session_id)
        monkeypatch.setattr(h, "_settle", real_settle)
        first = {m.message_id for m in sink.messages}
        assert len(first) == 2
        clock.now += STALE + timedelta(seconds=1)
        moved = replace(deps, message_id_domain="relay.other.example")
        got = await h.deliver_transcript(moved, worker.connect, s.session_id)
        assert got is not None and got.sent == 2
        final = await rows(pg, s.session_id)
        assert {r.message_id for r in final} == first
        for mid in first:
            assert len(sink.by_message_id(mid)) == 2


async def test_an_address_removed_while_waiting_for_a_session_is_not_mailed(
    pg, worker, sink
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit round 1 (additional, handler.py:361): the configuration is read again
    right before each row's claim; an address the teacher removed while the job waited for
    a relay session is not mailed (its row is left for the next fan-out to REPLACE)."""
    import asyncio

    a, b = "a@unical.it", "b@unical.it"
    clock = Clock()
    async with world(pg) as w:
        s, ring, config, pair = await _ready(w, worker, (a, b))
        await clock.sync(pg)
        sender = SmtpSender(sink_config(sink, max_connections=1))
        sender.slot_wait_s = 10
        deps = _deps(ring, config, sink, clock, sender=sender)
        held, release = asyncio.Event(), asyncio.Event()

        async def hog() -> None:
            async with sender.slot():
                held.set()
                await release.wait()

        hogging = asyncio.create_task(hog())
        await held.wait()
        job = asyncio.create_task(
            h.deliver_transcript(deps, worker.connect, s.session_id)
        )
        await asyncio.sleep(0.5)  # the job has fanned out and waits for the session
        await configure_recipients(pg, w.site, s.cmid, (a,), pair=pair)
        release.set()
        await hogging
        got = await job
        assert got is not None and got.sent == 1
        assert len(sink.to(a)) == 1 and sink.to(b) == []
        by = {r.recipient_ref: r for r in await rows(pg, s.session_id)}
        left = by[recipient_ref(w.site, b)]
        assert (left.status, left.attempts) == ("PENDING", 0)
        clock.advance(minutes=1)
        await h.deliver_transcript(deps, worker.connect, s.session_id)
        by = {r.recipient_ref: r.status for r in await rows(pg, s.session_id)}
        assert by[recipient_ref(w.site, b)] == "REPLACED" and sink.to(b) == []
