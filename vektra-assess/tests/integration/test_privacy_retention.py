"""Plan 12a: retention purge, subject and activity erasure, export, privacy settings, through
the real database roles (testcontainers) and the real routes.

"Purged" is never read from a status: every destructive test counts the rows of every content
table for the affected ids, asserts the count was > 0 BEFORE (no vacuous pass), 0 after, and
that a control subject's counts did not move.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import PASSING_FACTS, FakeSource
from support.recipients import configure_recipients
from support.sessions import Seeded, seed_session
from support.world import auth, default_settings, err, make_key, world

from integration.test_session_preflight import _reset
from integration.test_session_state import render
from integration.test_session_state import state as get_state
from vektra_assess.crypto.envelope import decrypt_turn
from vektra_assess.crypto.kek import unwrap_dek
from vektra_assess.errors import TRANSCRIPT_EXPIRED_TEXT
from vektra_assess.keys import AssessKeyring
from vektra_assess.privacy import config as privacy_config
from vektra_assess.privacy import purge as purge_mod
from vektra_assess.privacy.erasure import run_erasure
from vektra_assess.privacy.purge import PurgeVerificationFailed, run_purge
from vektra_assess.transcript import store
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

pytestmark = pytest.mark.integration

SETTINGS = {
    "retention_days": 30,
    "legal_basis_label": "Art. 6(1)(e) GDPR",
    "legal_basis_text": "Valutazione dell'apprendimento, compito di interesse pubblico.",
    "notice_url": "https://uni.example/privacy/discussione",
    "notice_text": "Informativa sul trattamento dei dati della discussione.",
    "notice_version": "2026-11",
}

#: the content of a session, by the column that ties it to the session (or its transcript)
SESSION_TABLES = (*purge_mod.SESSION_TABLES,)
TRANSCRIPT_TABLES = (*purge_mod.TRANSCRIPT_TABLES, "assess_transcripts")


class Facts:
    async def close_facts(self, **_: Any) -> CloseFacts:
        return CloseFacts(course_shortname="EA 2026/27")


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def one(pg, sql: str, **p: Any) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, p))[0][0])


async def content_counts(pg, sid: uuid.UUID) -> dict[str, int]:  # type: ignore[no-untyped-def]
    out: dict[str, int] = {}
    for table in SESSION_TABLES:
        out[table] = await one(
            pg, f"SELECT count(*) FROM {table} WHERE session_id = :s", s=sid
        )
    out["assess_transcripts"] = await one(
        pg, "SELECT count(*) FROM assess_transcripts WHERE session_id = :s", s=sid
    )
    for table in purge_mod.TRANSCRIPT_TABLES:
        out[table] = await one(
            pg, f"SELECT count(*) FROM {table} WHERE transcript_id = :s", s=sid
        )
    return out


async def ready(w, worker, **seed: Any) -> tuple[Seeded, AssessKeyring]:  # type: ignore[no-untyped-def]
    """A CLOSED session with its generated transcript (and its delivery rows)."""
    ring = w.app.state.assess_keys.get(store.KEYRING)
    if ring is None:
        ring = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
        w.app.state.assess_keys[store.KEYRING] = ring
    s = await seed_session(w, worker, "CLOSED", **seed)
    recipients = await configure_recipients(w.pg, w.site, s.cmid)
    got = await generate_transcript(
        GenerationDeps(
            kek=s.kek, transcript_kek=ring, facts=Facts(), recipients=recipients
        ),
        worker.connect,
        s.session_id,
    )
    assert got is not None and got.created
    return s, ring


async def as_other_student(pg, seeded: Seeded, userid: int) -> Seeded:  # type: ignore[no-untyped-def]
    """The recordings serve one graded student: a second subject is the same seeded session
    handed to another user id (every per-student row with it), so a control has real content."""
    for table in (
        "assess_sessions",
        "assess_start_attempts",
        "assess_token_jti",
        "assess_gate_inputs",
        "assess_render_state",
        "assess_render_checks",
    ):
        await pg.run(
            "super",
            f"UPDATE {table} SET student_userid = :n WHERE site_id = (SELECT site_id FROM "
            "assess_sessions WHERE id = :sid) AND cmid = :c AND student_userid = :u",
            {"n": userid, "sid": seeded.session_id, "c": seeded.cmid, "u": seeded.sub},
        )
    return Seeded(
        seeded.session_id, userid, seeded.cmid, seeded.session_token, seeded.kek
    )


async def configure_privacy(pg, site, **over: Any) -> None:  # type: ignore[no-untyped-def]
    doc = {**SETTINGS, **over}
    await pg.run(
        "super",
        "INSERT INTO assess_privacy_config (site_id, retention_days, legal_basis_label, "
        "legal_basis_text, notice_url, notice_text, notice_version, updated_by) VALUES "
        "(:s, :retention_days, :legal_basis_label, :legal_basis_text, :notice_url, "
        ":notice_text, :notice_version, 'test') ON CONFLICT (site_id) DO UPDATE SET "
        "retention_days = EXCLUDED.retention_days",
        {"s": site, **doc},
    )


async def age(pg, sid: uuid.UUID, days: int) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        f"UPDATE assess_sessions SET closed_at = now() - interval '{days} days' WHERE id = :s",
        {"s": sid},
    )


# --- the retention purge ----------------------------------------------------------------------


async def test_the_purge_removes_every_content_row_and_keeps_the_control(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        target, ring = await ready(w, worker, assignment="rubric_vis")
        control, _ = await ready(w, worker, assignment="scale")
        await configure_privacy(pg, w.site, retention_days=2)
        await age(pg, target.session_id, 3)
        await age(pg, control.session_id, 1)  # inside its retention
        # one copy still waiting for the relay: the purge abandons it and tells the graders
        await pg.run(
            "super",
            "UPDATE assess_deliveries SET status = 'RETRYING' WHERE transcript_id = :t",
            {"t": target.session_id},
        )
        before = await content_counts(pg, target.session_id)
        for table in ("assess_turns", "assess_snapshots", "assess_session_keys",
                      "assess_transcripts", "assess_deliveries"):  # fmt: skip
            assert before[table] > 0, table  # no vacuous pass
        control_before = await content_counts(pg, control.session_id)
        assert control_before["assess_turns"] > 0

        report = await run_purge(worker.connect)

        assert report.sessions == 1 and report.abandoned >= 1
        after = await content_counts(pg, target.session_id)
        assert after == dict.fromkeys(after, 0), after
        assert await content_counts(pg, control.session_id) == control_before
        # the session row stays (ids and numbers, no content), with one tombstone
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s",
                         s=target.session_id) == 1  # fmt: skip
        tomb = (
            await pg.run(
                "super",
                "SELECT reason, transcript_id, session_no, counts FROM assess_purge_records "
                "WHERE session_id = :s",
                {"s": target.session_id},
            )
        )[0]
        assert tomb[0] == "retention" and tomb[1] == target.session_id
        assert tomb[3]["assess_turns"] == before["assess_turns"]
        assert await one(pg, "SELECT count(*) FROM assess_purge_records WHERE session_id = :s",
                         s=control.session_id) == 0  # fmt: skip
        # audited, content-free; the graders' notice is queued with ids only
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_purge' "
                "AND site_id = :s AND (data->>'sessions')::int = 1",
                s=w.site,
            )
            == 1
        )
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE "
                "event = 'assess_delivery_abandoned_on_purge' AND session_id = :s",
                s=target.session_id,
            )
            >= 1
        )
        job = (
            await pg.run(
                "super",
                "SELECT args FROM assess_jobs WHERE kind = 'teacher_notify' "
                "AND args->>'reason' = 'delivery_abandoned_on_purge' "
                "AND args->>'site_id' = :s",
                {"s": str(w.site)},
            )
        )[0][0]
        assert job["student_userid"] == target.sub and job["cmid"] == target.cmid
        assert set(job) == {"site_id", "cmid", "student_userid", "session_no", "reason"}

        # a second run finds nothing more and changes nothing
        again = await run_purge(worker.connect)
        assert again.sessions == 0

        # the student learns it expired: /state says purged, the link answers 410
        r = await w.mint(sub=target.sub, cmid=target.cmid)
        state = await w.client.get(
            "/api/v1/assess/state",
            headers={"Authorization": f"Bearer {r.json()['token']}"},
        )
        assert state.status_code == 200, state.text
        listed = state.json()["transcripts"]
        assert listed == [
            {"tid": str(target.session_id), "n": 1, "available": False, "purged": True}
        ]
        got = await w.client.post(
            f"/api/v1/assess/transcripts/{target.session_id}/download-token",
            json={"sub": target.sub},
            headers=auth("mint"),
        )
        assert got.status_code == 410, got.text
        assert err(got)["code"] == "ERR-ASSESS-026"
        assert err(got)["message"] == TRANSCRIPT_EXPIRED_TEXT
        # another student asking for that id learns nothing
        other = await w.client.post(
            f"/api/v1/assess/transcripts/{target.session_id}/download-token",
            json={"sub": target.sub + 1},
            headers=auth("mint"),
        )
        assert other.status_code == 404, other.text
        # neither does a grader: the purged session's facts are gone, nothing can be verified
        grader = await w.client.post(
            f"/api/v1/assess/transcripts/{target.session_id}/download-token",
            json={"actor_userid": 12},
            headers=auth("teacher"),
        )
        assert grader.status_code == 404, grader.text
        del ring


async def test_crypto_shredding_leaves_no_key_for_a_saved_ciphertext(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await ready(w, worker)
        await configure_privacy(pg, w.site, retention_days=1)
        await age(pg, s.session_id, 2)
        key = (
            await pg.run(
                "super",
                "SELECT kek_wrapped, key_version FROM assess_session_keys WHERE session_id = :s",
                {"s": s.session_id},
            )
        )[0]
        turn = (
            await pg.run(
                "super",
                "SELECT turn_no, ciphertext FROM assess_turns WHERE session_id = :s "
                "ORDER BY turn_no LIMIT 1",
                {"s": s.session_id},
            )
        )[0]
        saved = bytes(turn[1])
        dek = unwrap_dek(s.kek, bytes(key[0]), s.session_id, int(key[1]))
        assert decrypt_turn(dek, s.session_id, int(turn[0]), saved)  # readable before

        await run_purge(worker.connect)

        # the only wrapped copy of the DEK is gone: the live KEK alone opens nothing
        assert await one(pg, "SELECT count(*) FROM assess_session_keys WHERE session_id = :s",
                         s=s.session_id) == 0  # fmt: skip
        assert await one(pg, "SELECT count(*) FROM assess_session_handoff WHERE session_id = :s",
                         s=s.session_id) == 0  # fmt: skip
        with pytest.raises(InvalidTag):
            decrypt_turn(s.kek.active_key, s.session_id, int(turn[0]), saved)


async def test_a_failed_count_rolls_the_batch_back(pg, worker, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await ready(w, worker)
        await configure_privacy(pg, w.site, retention_days=1)
        await age(pg, s.session_id, 2)
        before = await content_counts(pg, s.session_id)

        async def left(*_a: Any, **_k: Any) -> dict[str, int]:
            return {"assess_turns": 1}

        monkeypatch.setattr(purge_mod, "leftover", left)
        with pytest.raises(PurgeVerificationFailed):
            await run_purge(worker.connect)
        assert await content_counts(pg, s.session_id) == before
        assert await one(pg, "SELECT count(*) FROM assess_purge_records WHERE session_id = :s",
                         s=s.session_id) == 0  # fmt: skip
        assert await one(pg, "SELECT count(*) FROM assess_audit_events WHERE "
                         "event = 'assess_purge' AND site_id = :s", s=w.site) == 0  # fmt: skip


async def test_a_purged_transcript_is_never_regenerated(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, ring = await ready(w, worker)
        await configure_privacy(pg, w.site, retention_days=1)
        await age(pg, s.session_id, 2)
        await run_purge(worker.connect)
        recipients = await configure_recipients(w.pg, w.site, s.cmid)
        got = await generate_transcript(
            GenerationDeps(
                kek=s.kek, transcript_kek=ring, facts=Facts(), recipients=recipients
            ),
            worker.connect,
            s.session_id,
        )
        assert got is None
        assert await one(pg, "SELECT count(*) FROM assess_transcripts WHERE session_id = :s",
                         s=s.session_id) == 0  # fmt: skip


async def test_a_site_without_privacy_settings_is_never_purged(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await ready(w, worker)
        await age(pg, s.session_id, 4000)
        before = await content_counts(pg, s.session_id)
        await run_purge(worker.connect)
        assert await content_counts(pg, s.session_id) == before


# --- erasure ----------------------------------------------------------------------------------


async def _privacy_key(w, scope: str, name: str, site=None) -> None:  # type: ignore[no-untyped-def]
    purpose = scope.removeprefix("assess:")
    info = await make_key(w.pg, [scope], site or w.site, purpose)
    w.app.state.registry.get("key_store", "default").table[name] = info


async def _work_erasures(pg, worker, stop: asyncio.Event) -> None:  # type: ignore[no-untyped-def]
    """What the worker does: complete every queued erasure request."""
    while not stop.is_set():
        rows = await pg.run(
            "super",
            "SELECT (args->>'erasure_id')::bigint FROM assess_jobs WHERE kind = 'erasure' "
            "AND state = 'ready'",
        )
        for (eid,) in rows:
            await pg.run("super", "UPDATE assess_jobs SET state = 'done', done_at = now() "
                         "WHERE kind = 'erasure' AND (args->>'erasure_id')::bigint = :i",
                         {"i": eid})  # fmt: skip
            await run_erasure(worker.connect, int(eid))
        await asyncio.sleep(0.05)


async def erase(w, worker, path: str, key: str = "erase", **kw: Any):  # type: ignore[no-untyped-def]
    stop = asyncio.Event()
    task = asyncio.create_task(_work_erasures(w.pg, worker, stop))
    try:
        return await w.client.request("DELETE", path, headers=auth(key), **kw)
    finally:
        stop.set()
        await task


async def subject_counts(pg, site, user: int) -> dict[str, int]:  # type: ignore[no-untyped-def]
    out = {}
    for table in ("assess_sessions", "assess_start_attempts", "assess_token_jti",
                  "assess_gate_inputs", "assess_render_state", "assess_extra_time",
                  "assess_reset_grants"):  # fmt: skip
        out[table] = await one(
            pg,
            f"SELECT count(*) FROM {table} WHERE site_id = :s AND student_userid = :u",
            s=site,
            u=user,
        )
    return out


async def test_erasure_removes_the_subject_and_only_the_subject(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=10)) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase")
        control, _ = await ready(w, worker, assignment="rubric_vis")
        control = await as_other_student(pg, control, 990001)
        closed, _ = await ready(w, worker, assignment="rubric_vis")
        active = await seed_session(w, worker, "ACTIVE", assignment="simple")
        await pg.run(
            "super",
            "INSERT INTO assess_extra_time (site_id, cmid, student_userid, extra_seconds) "
            "VALUES (:s, :c, :u, 600)",
            {"s": w.site, "c": closed.cmid, "u": closed.sub},
        )
        # queued work that would write the subject's rows back after the erasure
        from vektra_assess.jobs import JobKind, enqueue_job, render_dedupe_key

        async with worker.begin() as conn:
            for user in (closed.sub, control.sub):
                await enqueue_job(
                    conn,
                    JobKind.RENDER_CHECK,
                    {
                        "site_id": str(w.site),
                        "student_userid": user,
                        "cmid": closed.cmid,
                    },
                    dedupe_key=render_dedupe_key(w.site, user, closed.cmid),
                )
        before = await subject_counts(pg, w.site, closed.sub)
        assert before["assess_sessions"] == 2 and before["assess_extra_time"] == 1
        sessions_before = {
            sid: await content_counts(pg, sid)
            for sid in (closed.session_id, active.session_id)
        }
        assert sessions_before[closed.session_id]["assess_transcripts"] == 1
        assert sessions_before[active.session_id]["assess_turns"] > 0
        control_before = await content_counts(pg, control.session_id)

        r = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{closed.sub}")

        assert r.status_code == 200, r.text
        assert r.json() == {
            "outcome": "erased",
            "counts": {"sessions": 2, "transcripts": 1, "deliveries_abandoned": 2},
        }
        assert await subject_counts(pg, w.site, closed.sub) == dict.fromkeys(before, 0)
        for sid in (closed.session_id, active.session_id):
            after = await content_counts(pg, sid)
            assert after == dict.fromkeys(after, 0), (sid, after)
        assert await content_counts(pg, control.session_id) == control_before
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s",
                         s=control.session_id) == 1  # fmt: skip
        # the two copies still waiting for the relay were abandoned, each audited
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE "
                "event = 'assess_delivery_abandoned_on_purge' AND session_id = :s "
                "AND reason_code = 'subject_erased'",
                s=closed.session_id,
            )
            == 2
        )
        # no grader notice for an erasure (the subject asked for its data to go)
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'teacher_notify' "
                "AND args->>'site_id' = :s",
                s=str(w.site),
            )
            == 0
        )
        # the live session went through the single close path first
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_close' "
                "AND session_id = :s AND reason_code = 'access_revoked'",
                s=active.session_id,
            )
            == 1
        )
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_erasure' "
                "AND site_id = :s AND (data->>'sessions')::int = 2",
                s=w.site,
            )
            == 1
        )
        # no queued job is left to write the subject back; the control's job stays
        jobs = await pg.run(
            "super",
            "SELECT args->>'student_userid' FROM assess_jobs WHERE kind = 'render_check' "
            "AND args->>'site_id' = :s AND state = 'ready'",
            {"s": str(w.site)},
        )
        assert [j[0] for j in jobs] == [str(control.sub)]
        # the tombstones lead back to nobody
        refs = await pg.run(
            "super",
            "SELECT DISTINCT subject_ref FROM assess_purge_records WHERE site_id = :s",
            {"s": w.site},
        )
        assert refs == [("erased",)]
        # the request record keeps counts, neither the user id nor its hash
        rec = await pg.run(
            "super",
            "SELECT outcome, subject_userids, subject_userid_hash, completed_at IS NOT NULL "
            "FROM assess_erasure_records WHERE site_id = :s",
            {"s": w.site},
        )
        assert rec == [("erased", None, None, True)]

        # idempotent: a repeat erases nothing and still answers erased
        again = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{closed.sub}")
        assert again.status_code == 200, again.text
        assert again.json()["counts"] == {
            "sessions": 0, "transcripts": 0, "deliveries_abandoned": 0,
        }  # fmt: skip


async def test_retain_mode_deletes_nothing_and_says_so(pg, worker) -> None:  # type: ignore[no-untyped-def]
    cfg = default_settings(VEKTRA_ASSESS_ERASURE_MODE="retain_exam_evidence")
    async with world(pg, cfg) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase")
        await configure_privacy(pg, w.site)
        s, _ = await ready(w, worker)
        before = await content_counts(pg, s.session_id)
        r = await w.client.delete(
            f"/api/v1/assess/privacy/subjects/{s.sub}", headers=auth("erase")
        )
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "retained"
        assert r.json()["legal_basis"] == SETTINGS["legal_basis_label"]
        assert await content_counts(pg, s.session_id) == before
        assert (
            await one(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'erasure'")
            == 0
        )
        assert await one(pg, "SELECT count(*) FROM assess_audit_events WHERE "
                         "event = 'assess_erasure_retained' AND site_id = :s", s=w.site) == 1  # fmt: skip


async def test_an_unfinished_erasure_is_a_503_never_a_success(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=0.5)) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase")
        s, _ = await ready(w, worker)
        r = await w.client.delete(
            f"/api/v1/assess/privacy/subjects/{s.sub}", headers=auth("erase")
        )  # no worker runs
        assert r.status_code == 503, r.text
        assert err(r)["code"] == "ERR-ASSESS-027"
        assert err(r)["details"] == {"reason": "pending"}
        assert r.headers["retry-after"]
        # the plugin's retry waits on the same request: no second row carrying the same id
        r = await w.client.delete(
            f"/api/v1/assess/privacy/subjects/{s.sub}", headers=auth("erase")
        )
        assert r.status_code == 503, r.text
        assert await one(pg, "SELECT count(*) FROM assess_erasure_records WHERE site_id = :s",
                         s=w.site) == 1  # fmt: skip
        assert (
            await one(pg, "SELECT count(*) FROM assess_jobs WHERE kind = 'erasure'")
            == 1
        )
        # a request whose job died is recorded failed by the purge tick a day later, its ids
        # dropped
        await pg.run("super", "UPDATE assess_erasure_records SET requested_at = now() - "
                     "interval '2 days' WHERE site_id = :s", {"s": w.site})  # fmt: skip
        await run_purge(worker.connect)
        rec = await pg.run(
            "super",
            "SELECT outcome, error_code, subject_userids, subject_userid_hash "
            "FROM assess_erasure_records WHERE site_id = :s",
            {"s": w.site},
        )
        assert rec == [("failed", "abandoned", None, None)]


async def test_activity_listing_and_erasure_stay_inside_the_activity(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=10)) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase")
        await _privacy_key(w, "assess:privacy-export", "export")
        b, _ = await ready(w, worker, assignment="rubric_vis")
        b = await as_other_student(pg, b, 990002)
        a, _ = await ready(w, worker, assignment="rubric_vis")
        elsewhere, _ = await ready(w, worker, assignment="scale")
        # a student who only opened the page (the render worker's rows, no session)
        await pg.run(
            "super",
            "INSERT INTO assess_render_state (site_id, student_userid, cmid, status) "
            "VALUES (:s, 990009, :c, 'eligible')",
            {"s": w.site, "c": a.cmid},
        )
        listed = await w.client.get(
            f"/api/v1/assess/privacy/activities/{a.cmid}/subjects",
            headers=auth("export"),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["userids"] == sorted({a.sub, b.sub, 990009})
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE "
                "event = 'assess_privacy_subjects_listed' AND site_id = :s "
                "AND (data->>'subjects')::int = 3",
                s=w.site,
            )
            == 1
        )
        keep = await content_counts(pg, elsewhere.session_id)

        r = await erase(
            w, worker, f"/api/v1/assess/privacy/activities/{a.cmid}/subjects",
            json={"userids": [a.sub]},
        )  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["counts"]["sessions"] == 1
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s",
                         s=a.session_id) == 0  # fmt: skip
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s",
                         s=b.session_id) == 1  # fmt: skip
        assert await content_counts(pg, elsewhere.session_id) == keep

        # the whole activity, nothing else
        r = await erase(
            w, worker, f"/api/v1/assess/privacy/activities/{a.cmid}/subjects"
        )
        assert r.status_code == 200, r.text
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE site_id = :s "
                         "AND cmid = :c", s=w.site, c=a.cmid) == 0  # fmt: skip
        assert await content_counts(pg, elsewhere.session_id) == keep


async def test_privacy_routes_take_only_their_own_site_bound_key(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase_other", site=w.other_site)
        s, _ = await ready(w, worker)
        for key in ("admin", "mint", "teacher"):
            r = await w.client.delete(
                f"/api/v1/assess/privacy/subjects/{s.sub}", headers=auth(key)
            )
            assert r.status_code == 403, (key, r.text)
            r = await w.client.get(
                f"/api/v1/assess/privacy/subjects/{s.sub}/transcripts",
                headers=auth(key),
            )
            assert r.status_code == 403, (key, r.text)
        # another site's key erases in its own site only: nothing here moves
        before = await content_counts(pg, s.session_id)
        r = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{s.sub}",
                        key="erase_other")  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["counts"]["sessions"] == 0
        assert await content_counts(pg, s.session_id) == before


# --- export -----------------------------------------------------------------------------------


async def test_export_returns_the_exact_bytes_whatever_the_enrolment(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        await _privacy_key(w, "assess:privacy-export", "export")
        other, _ = await ready(w, worker, assignment="rubric_vis")
        other = await as_other_student(pg, other, 990003)
        s, ring = await ready(w, worker, assignment="scale")
        # the student has left the course: the worker's facts say so; export does not care
        await pg.run(
            "super", "DELETE FROM assess_gate_inputs WHERE site_id = :s", {"s": w.site}
        )
        r = await w.client.get(
            f"/api/v1/assess/privacy/subjects/{s.sub}/transcripts",
            headers=auth("export"),
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [i["transcript_id"] for i in items] == [str(s.session_id)]
        body = base64.b64decode(items[0]["content_base64"])
        row = (
            await pg.run(
                "super",
                "SELECT ciphertext, dek_wrapped, content_hash FROM assess_transcripts "
                "WHERE id = :t",
                {"t": s.session_id},
            )
        )[0]
        assert body == store.open_verified(
            ring, s.session_id, bytes(row[0]), bytes(row[1]), row[2]
        )
        assert hashlib.sha256(body).hexdigest() == items[0]["sha256"] == row[2]
        assert str(other.session_id) not in r.text
        assert await one(pg, "SELECT count(*) FROM assess_audit_events WHERE "
                         "event = 'assess_privacy_export' AND site_id = :s", s=w.site) == 1  # fmt: skip

        # after the purge: nothing left to export
        await configure_privacy(pg, w.site, retention_days=1)
        await age(pg, s.session_id, 2)
        await run_purge(worker.connect)
        r = await w.client.get(
            f"/api/v1/assess/privacy/subjects/{s.sub}/transcripts",
            headers=auth("export"),
        )
        assert r.status_code == 200 and r.json()["items"] == []


# --- privacy settings (REQ-089) ---------------------------------------------------------------


async def test_settings_are_written_by_the_operator_and_gate_everything(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        url = f"/api/v1/assess/sites/{w.site}/privacy"
        async with w.engine.connect() as conn:
            assert await privacy_config.is_configured(conn, w.site) is False
        bad = await w.client.put(
            url, json={**SETTINGS, "retention_days": 0, "notice_url": "http://x.example"},
            headers=auth("admin"),
        )  # fmt: skip
        assert bad.status_code == 400, bad.text
        assert err(bad)["code"] == "ERR-ASSESS-028"
        fields = {f["field"]: f["code"] for f in err(bad)["details"]["fields"]}
        assert fields == {"retention_days": "out_of_range", "notice_url": "not_https"}
        assert "http://x.example" not in bad.text
        for key in ("teacher", "mint"):
            r = await w.client.put(url, json=SETTINGS, headers=auth(key))
            assert r.status_code == 403, (key, r.text)

        ok = await w.client.put(url, json=SETTINGS, headers=auth("admin"))
        assert ok.status_code == 200, ok.text
        assert ok.json()["configured"] is True
        assert ok.json()["mailbox_notice"] == privacy_config.MAILBOX_SENTENCE
        async with w.engine.connect() as conn:
            assert await privacy_config.is_configured(conn, w.site) is True
            assert (
                await privacy_config.current_notice_version(conn, w.site) == "2026-11"
            )
        audit = (
            await pg.run(
                "super",
                "SELECT reason_code, data FROM assess_audit_events WHERE "
                "event = 'assess_privacy_config_changed' AND site_id = :s",
                {"s": w.site},
            )
        )[0]
        assert audit[0] == "created" and audit[1]["new_retention_days"] == 30
        assert SETTINGS["legal_basis_text"] not in str(audit[1])

        # the Start records the site's notice version, the pre-session summary its retention
        # and the fixed mailbox sentence
        s = await seed_session(w, worker, "ELIGIBLE")
        for table in (
            "assess_render_checks",
            "assess_render_state",
            "assess_gate_inputs",
        ):
            await pg.run(
                "super", f"DELETE FROM {table} WHERE site_id = :s", {"s": w.site}
            )
        await render(w, worker, s.sub, s.cmid, FakeSource(facts=PASSING_FACTS))
        state = await get_state(w, await w.presession(sub=s.sub, cmid=s.cmid))
        assert state.json()["state"] == "eligible", state.text
        privacy = state.json()["pre_session"]["privacy"]
        assert privacy["notice_version"] == "2026-11"
        assert privacy_config.MAILBOX_SENTENCE in privacy["summary"]
        assert "30 giorni" in privacy["summary"]
        started = await seed_session(w, worker, "STARTING")
        nv = (
            await pg.run(
                "super",
                "SELECT notice_version FROM assess_sessions WHERE id = :s",
                {"s": started.session_id},
            )
        )[0][0]
        assert nv == "2026-11"
        assert (
            await one(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_start' "
                "AND session_id = :s AND data->>'notice_version' = '2026-11'",
                s=started.session_id,
            )
            == 1
        )


async def test_an_erasure_blanks_the_subject_of_earlier_retention_tombstones(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg, default_settings(VEKTRA_ASSESS_ERASURE_WAIT_S=10)) as w:
        await _privacy_key(w, "assess:privacy-erase", "erase")
        s, _ = await ready(w, worker)
        await configure_privacy(pg, w.site, retention_days=1)
        await age(pg, s.session_id, 2)
        await run_purge(worker.connect)
        ref = (
            await pg.run(
                "super",
                "SELECT subject_ref FROM assess_purge_records WHERE session_id = :s",
                {"s": s.session_id},
            )
        )[0][0]
        assert ref != "erased"  # a retention tombstone names its subject (for the 410)
        r = await erase(w, worker, f"/api/v1/assess/privacy/subjects/{s.sub}")
        assert r.status_code == 200, r.text
        assert r.json()["counts"]["sessions"] == 1  # the emptied session row itself
        rows = await pg.run(
            "super",
            "SELECT reason, subject_ref FROM assess_purge_records WHERE session_id = :s",
            {"s": s.session_id},
        )
        assert rows == [("retention", "erased")]
        assert await one(pg, "SELECT count(*) FROM assess_sessions WHERE id = :s",
                         s=s.session_id) == 0  # fmt: skip
