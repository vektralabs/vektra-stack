"""Plan 10: ``transcript_generate`` on a real database, with sessions seeded through the real code.

Evidence is counted, never read from a status: a "generated" transcript is decrypted and its bytes
re-hashed against the stored hash, then re-checked by the allowlist; a tampered turn yields 0 rows;
two concurrent jobs yield 1 row. Hostile turn content (XSS, a markdown image, a remote URL,
header-like text) is posted through the real turn route and the produced file is opened in
Chromium with network logging (``test_transcript_inert_browser.py``).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset, moodle_source
from vektra_assess.context.regrade import GradeReading
from vektra_assess.crypto import envelope
from vektra_assess.crypto.kek import unwrap_dek
from vektra_assess.keys import AssessKeyring
from vektra_assess.readable import Readable
from vektra_assess.transcript import allowlist, naming, store
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import (
    GenerationDeps,
    IntegrityFailure,
    generate_transcript,
)

pytestmark = pytest.mark.integration

STUDENT_NAMES = ("Alice Alpha", "Bruno Beta")


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def transcript_ring() -> AssessKeyring:
    return AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})


class Facts:
    def __init__(self, inner: Any = None, facts: CloseFacts | None = None) -> None:
        self.inner, self.facts, self.calls = inner, facts, 0

    async def close_facts(self, **kw: Any) -> CloseFacts:
        self.calls += 1
        if self.facts is not None:
            return self.facts
        return await self.inner.close_facts(**kw)  # type: ignore[no-any-return]


async def _closed(w, worker, **kw: Any):  # type: ignore[no-untyped-def]
    return await seed_session(w, worker, "CLOSED", **kw)


async def _open(pg, ring: AssessKeyring, tid: uuid.UUID) -> bytes:  # type: ignore[no-untyped-def]
    row = (
        await pg.run(
            "api",
            "SELECT ciphertext, dek_wrapped, content_hash FROM assess_transcripts WHERE id = :t",
            {"t": tid},
        )
    )[0]
    return store.open_verified(
        ring, tid, bytes(row.ciphertext), bytes(row.dek_wrapped), row.content_hash
    )


async def count(pg, sql: str, **p: Any) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, p))[0][0])


async def test_a_closed_session_gets_one_verified_transcript_and_recipient_rows(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(moodle_source(worker))
        )
        got = await generate_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.created and got.transcript_id == s.session_id
        # evidence: decrypt and re-hash (open_verified raises on any mismatch)
        doc = await _open(pg, ring, s.session_id)
        allowlist.check(doc)
        html = doc.decode()
        assert "EAASSESS" in html and "Sessione #1 di 1" in html
        for name in STUDENT_NAMES:
            assert name not in html  # identifiers only (OQ-020)
        # canaries (REQ-074): no system prompt, no context framing, no hidden-grade text
        for canary in ("Sei l'assistente di Vektra", "CONTESTO DELLA PROVA", "FONTI."):
            assert canary not in html and canary.replace("'", "&#39;") not in html
        row = (
            await pg.run(
                "super",
                "SELECT filename, byte_size, turns_digest FROM assess_transcripts WHERE id = :t",
                {"t": s.session_id},
            )
        )[0]
        assert naming.PATTERN.match(row.filename) and row.filename.startswith(
            f"EAASSESS_{s.cmid}_{s.sub}_s1_"
        )
        assert row.byte_size == len(doc) and row.turns_digest in html
        # the course's graders (fixtures: 2, the student excluded), PENDING, round 1
        rows = await pg.run(
            "super",
            "SELECT recipient_userid, status, round, recipient_kind FROM assess_deliveries "
            "WHERE transcript_id = :t ORDER BY recipient_userid",
            {"t": s.session_id},
        )
        assert len(rows) == 2 and {r.status for r in rows} == {"PENDING"}
        assert {r.round for r in rows} == {1} and {r.recipient_kind for r in rows} == {
            "grader"
        }
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = "
                "'assess_transcript_generated' AND session_id = :s",
                s=s.session_id,
            )
            == 1
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_deliver' "
                "AND args->>'transcript_id' = :t",
                t=str(s.session_id),
            )
            == 1
        )
        # a second job is a no-op that completes: still one row, no second delivery set
        assert await generate_transcript(deps, worker.connect, s.session_id) is None
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t",
                t=s.session_id,
            )
            == 2
        )


async def test_two_concurrent_jobs_make_one_row(pg, worker) -> None:  # type: ignore[no-untyped-def]
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        results = await asyncio.gather(
            *(generate_transcript(deps, worker.connect, s.session_id) for _ in range(4))
        )
        assert sum(1 for r in results if r is not None and r.created) == 1
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcripts WHERE session_id = :s",
                s=s.session_id,
            )
            == 1
        )


async def test_a_tampered_turn_yields_no_transcript_and_an_alert(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """REQ-102: a DB writer re-encrypts turn 2 with the session DEK (the AEAD verifies) and keeps
    the stored hash: the re-hash at generation catches it. No transcript, audited, dead job."""
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        key = (
            await pg.run(
                "super",
                "SELECT kek_wrapped, key_version FROM assess_session_keys WHERE session_id = :s",
                {"s": s.session_id},
            )
        )[0]
        dek = unwrap_dek(s.kek, bytes(key.kek_wrapped), s.session_id, key.key_version)
        await pg.run(
            "super",
            "UPDATE assess_turns SET ciphertext = :c WHERE session_id = :s AND turn_no = 2",
            {
                "c": envelope.encrypt_turn(dek, s.session_id, 2, b"edited"),
                "s": s.session_id,
            },
        )
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        with pytest.raises(IntegrityFailure) as exc:
            await generate_transcript(deps, worker.connect, s.session_id)
        assert exc.value.code == "integrity_mismatch" and exc.value.turn_no == 2
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcripts WHERE session_id = :s",
                s=s.session_id,
            )
            == 0
        )
        audit = await pg.run(
            "super",
            "SELECT reason_code, data FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_transcript_integrity_failed'",
            {"s": s.session_id},
        )
        assert [(a.reason_code, a.data) for a in audit] == [
            ("turn_hash_mismatch", {"turn_no": 2})
        ]


async def test_unreadable_graders_still_generate_without_delivery_rows(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        got = await generate_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.recipients == 0
        html = (await _open(pg, ring, s.session_id)).decode()
        assert "non disponibile" in html  # the course and the grade at close
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t",
                t=s.session_id,
            )
            == 0
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_transcript_recipients_unresolved'",
                s=s.session_id,
            )
            == 1
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_deliver' "
                "AND args->>'transcript_id' = :t",
                t=str(s.session_id),
            )
            == 0
        )


async def test_a_session_that_is_not_closed_is_a_stale_no_op(pg, worker) -> None:  # type: ignore[no-untyped-def]
    ring = transcript_ring()
    async with world(pg) as w:
        s = await seed_session(w, worker, "ACTIVE")
        deps = GenerationDeps(kek=s.kek or object(), transcript_kek=ring)  # type: ignore[arg-type]
        assert await generate_transcript(deps, worker.connect, s.session_id) is None
        assert await generate_transcript(deps, worker.connect, uuid.uuid4()) is None


async def test_every_closed_session_gets_exactly_one_transcript(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """REQ-074 count: transcripts == CLOSED sessions of the seeded set (three sites)."""
    ring = transcript_ring()
    seeded = []
    for _ in range(3):
        async with world(pg) as w:
            s = await _closed(w, worker)
            deps = GenerationDeps(
                kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
            )
            await generate_transcript(deps, worker.connect, s.session_id)
            seeded.append(s)
    ids = [s.session_id for s in seeded]
    closed = await count(
        pg,
        "SELECT count(*) FROM assess_sessions WHERE id = ANY(:i) AND state = 'CLOSED'",
        i=ids,
    )
    made = await count(
        pg, "SELECT count(*) FROM assess_transcripts WHERE session_id = ANY(:i)", i=ids
    )
    assert closed == made == 3
    for s in seeded:  # each one decrypts and re-hashes
        await _open(pg, ring, s.session_id)


async def test_a_tampered_snapshot_is_audited_as_a_transcript_integrity_failure(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review #3: a snapshot that no longer re-hashes to the chain's genesis writes the
    transcript integrity audit row (and no transcript)."""
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        # an actor with owner rights bypasses the BR-015 immutability trigger
        await pg.run("super", "ALTER TABLE assess_snapshots DISABLE TRIGGER USER")
        try:
            await pg.run(
                "super",
                "UPDATE assess_snapshots SET content_hash = repeat('0', 64) "
                "WHERE session_id = :s",
                {"s": s.session_id},
            )
        finally:
            await pg.run("super", "ALTER TABLE assess_snapshots ENABLE TRIGGER USER")
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        with pytest.raises(IntegrityFailure):
            await generate_transcript(deps, worker.connect, s.session_id)
        audit = await pg.run(
            "super",
            "SELECT reason_code FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_transcript_integrity_failed'",
            {"s": s.session_id},
        )
        assert [a[0] for a in audit] == ["snapshot_hash_mismatch"]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcripts WHERE session_id = :s",
                s=s.session_id,
            )
            == 0
        )


async def test_slow_moodle_facts_fall_back_inside_the_job_budget(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review #4: Moodle answering slower than its share of the budget yields the transcript
    with "non disponibile" fields, inside the job's own timeout (which would kill the job)."""
    import asyncio

    class Slow:
        async def close_facts(self, **_: Any) -> CloseFacts:
            await asyncio.sleep(30)
            return CloseFacts(course_shortname="NEVER")

    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Slow(), facts_timeout_s=0.3
        )
        async with asyncio.timeout(3):  # stands for the job budget
            got = await generate_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.created
        html = (await _open(pg, ring, s.session_id)).decode()
        assert "non disponibile" in html and "NEVER" not in html


async def test_a_missing_kek_kid_is_retryable_not_an_integrity_failure(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review #8: a KEK kid the worker does not hold is a configuration or rotation problem:
    a normal, retried job failure, with no integrity audit row and no transcript."""
    from vektra_assess.worker.runtime import PermanentJobError
    from vektra_assess.worker.transcript_gen import TranscriptKeyUnavailable

    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        other = AssessKeyring("assess_kek", "other", {"other": os.urandom(32)})
        deps = GenerationDeps(
            kek=other, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        with pytest.raises(TranscriptKeyUnavailable) as exc:
            await generate_transcript(deps, worker.connect, s.session_id)
        assert not isinstance(exc.value, PermanentJobError)
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE session_id = :s "
                "AND event = 'assess_transcript_integrity_failed'",
                s=s.session_id,
            )
            == 0
        )
        # with the right keyring the retry succeeds
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        got = await generate_transcript(deps, worker.connect, s.session_id, attempt=2)
        assert got is not None and got.created


async def test_close_facts_read_the_course_list_once(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review #10: the short name comes with the activity resolution (one
    core_enrol_get_users_courses), and the resolution is not left in the source's cache."""
    from assess_moodle_support import Replay, make_client
    from test_context_capture import InProcessParser

    from vektra_assess.worker.start_source import MoodleStartSource

    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        replay = Replay("4.3")
        src = MoodleStartSource(make_client(replay), InProcessParser(), worker.connect)
        deps = GenerationDeps(kek=s.kek, transcript_kek=ring, facts=src)
        assert await generate_transcript(deps, worker.connect, s.session_id)
        names = [c[0] for c in replay.calls]
        assert names.count("core_enrol_get_users_courses") == 1, names
        assert "EAASSESS" in (await _open(pg, ring, s.session_id)).decode()
        assert src.cached == 0


async def test_graders_resolve_after_the_student_left_the_course(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503025969: the student is no longer enrolled anywhere, yet the course's
    graders are read through the session's course hint, accepted because the cmid is found
    in that course; a hint naming a course without the cmid resolves nothing."""
    from assess_moodle_support import Replay, make_client, manifest
    from test_context_capture import InProcessParser

    from vektra_assess.worker.start_source import MoodleStartSource

    ring = transcript_ring()
    course = int(manifest("4.3")["courseid"])
    for hint, expect_rows in ((course, 2), (course + 999, 0)):
        async with world(pg) as w:
            s = await _closed(w, worker)
            await pg.run(
                "super",
                "UPDATE assess_sessions SET courseid_hint = :c WHERE id = :s",
                {"c": hint, "s": s.session_id},
            )
            replay = Replay("4.3")
            replay.mutate = lambda fn, body: (
                [] if fn == "core_enrol_get_users_courses" else body
            )
            src = MoodleStartSource(
                make_client(replay), InProcessParser(), worker.connect
            )
            deps = GenerationDeps(kek=s.kek, transcript_kek=ring, facts=src)
            assert await generate_transcript(deps, worker.connect, s.session_id)
            assert (
                await count(
                    pg,
                    "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t",
                    t=s.session_id,
                )
                == expect_rows
            )


async def test_the_header_shows_the_cap_frozen_at_start(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503025981: the teacher raises the cap from 30 to 60 minutes after the
    session started; the transcript still says 30 (and the granted extra is not hidden)."""
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        assert (
            await pg.run(
                "super",
                "SELECT cap_s FROM assess_sessions WHERE id = :s",
                {"s": s.session_id},
            )
        )[0][0] == 1800
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET cap_minutes = 60 WHERE site_id = :s AND cmid = :c",
            {"s": w.site, "c": s.cmid},
        )
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        await generate_transcript(deps, worker.connect, s.session_id)
        html = (await _open(pg, ring, s.session_id)).decode()
        assert "<dt>Durata massima</dt><dd>30 min</dd>" in html
        assert "<dt>Tempo aggiuntivo</dt><dd>nessuno</dd>" in html


async def test_a_deleted_trailing_turn_yields_no_transcript(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503029416: the prefix left after deleting the last turn still chains;
    the session's turn counter (seq) does not agree, so nothing is sealed."""
    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        last = (
            await pg.run(
                "super",
                "SELECT max(turn_no) FROM assess_turns WHERE session_id = :s",
                {"s": s.session_id},
            )
        )[0][0]
        await pg.run(
            "super",
            "DELETE FROM assess_turns WHERE session_id = :s AND turn_no = :n",
            {"s": s.session_id, "n": last},
        )
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
        )
        with pytest.raises(IntegrityFailure):
            await generate_transcript(deps, worker.connect, s.session_id)
        audit = await pg.run(
            "super",
            "SELECT reason_code FROM assess_audit_events WHERE session_id = :s "
            "AND event = 'assess_transcript_integrity_failed'",
            {"s": s.session_id},
        )
        assert [a[0] for a in audit] == ["turn_count_mismatch"]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcripts WHERE session_id = :s",
                s=s.session_id,
            )
            == 0
        )


async def test_a_grader_read_timeout_keeps_the_course_and_grade_already_read(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503029420: the course short name was read before the graders hung: the
    transcript keeps it (header and file name), only the recipients are missing."""
    import asyncio

    class Partial:
        async def close_facts(self, *, sink: dict[str, Any], **_: Any) -> CloseFacts:
            sink["course_shortname"] = "PARTIAL-COURSE"
            sink["grade"] = Readable(GradeReading(True, "27.25"))
            await asyncio.sleep(30)  # the grader read never answers
            return CloseFacts()

    ring = transcript_ring()
    async with world(pg) as w:
        s = await _closed(w, worker)
        deps = GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Partial(), facts_timeout_s=0.3
        )
        got = await generate_transcript(deps, worker.connect, s.session_id)
        assert got is not None and got.recipients == 0
        html = (await _open(pg, ring, s.session_id)).decode()
        assert "PARTIAL-COURSE" in html
        assert "27.25" in html or "27,25" in html  # the grade read before the hang
        name = (
            await pg.run(
                "super",
                "SELECT filename FROM assess_transcripts WHERE id = :t",
                {"t": s.session_id},
            )
        )[0][0]
        assert name.startswith("PARTIAL-COURSE_")
