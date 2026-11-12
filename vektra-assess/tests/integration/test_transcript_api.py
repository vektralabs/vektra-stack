"""Plan 10: download-token mint, single-use download and resend, through the real routes.

Every success is checked on the bytes: the downloaded body must re-hash to the stored SHA-256 and
equal the decrypted row, and the student's and the teacher's copies must be identical. Every
refusal is checked on its status AND on what it left behind (audit rows, consumed jtis, queued
refresh jobs).
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import configure_recipients, refs
from support.sessions import seed_session
from support.world import auth, err, world

from integration.test_session_preflight import _reset
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import naming, store
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

pytestmark = pytest.mark.integration


class Facts:
    async def close_facts(self, **_: Any) -> CloseFacts:
        return CloseFacts(course_shortname="EA 2026/27")


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


async def _ready(w, worker):  # type: ignore[no-untyped-def]
    """A CLOSED session with its generated transcript; the API holds the transcript key."""
    ring = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
    w.app.state.assess_keys[store.KEYRING] = ring
    s = await seed_session(w, worker, "CLOSED")
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


async def _graders(w, s, ids=(12, 13), age: str = "1 second") -> None:  # type: ignore[no-untyped-def]
    """What the render worker writes (plan 10): the course's grader ids, as the worker role."""
    import json

    await w.pg.run(
        "worker",
        "INSERT INTO assess_gate_inputs (site_id, student_userid, cmid, inputs, refreshed_at) "
        f"VALUES (:s, :u, :c, CAST(:i AS jsonb), now() - interval '{age}') "
        "ON CONFLICT (site_id, student_userid, cmid) DO UPDATE SET inputs = EXCLUDED.inputs, "
        "refreshed_at = EXCLUDED.refreshed_at",
        {"s": w.site, "u": s.sub, "c": s.cmid, "i": json.dumps({"graders": list(ids)})},
    )


async def _age_rounds(pg, s) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "UPDATE assess_deliveries SET created_at = now() - interval '1 hour' "
        "WHERE transcript_id = :t",
        {"t": s.session_id},
    )


async def mint(w, tid, key: str = "mint", **body: Any):  # type: ignore[no-untyped-def]
    return await w.client.post(
        f"/api/v1/assess/transcripts/{tid}/download-token", json=body, headers=auth(key)
    )


async def download(w, tid, token: str):  # type: ignore[no-untyped-def]
    return await w.client.get(
        f"/api/v1/assess/transcripts/{tid}",
        headers={"Authorization": f"Bearer {token}"},
    )


async def count(pg, sql: str, **p: Any) -> int:  # type: ignore[no-untyped-def]
    return int((await pg.run("super", sql, p))[0][0])


async def stored(pg, ring, tid) -> bytes:  # type: ignore[no-untyped-def]
    row = (
        await pg.run(
            "super",
            "SELECT ciphertext, dek_wrapped, content_hash FROM assess_transcripts WHERE id = :t",
            {"t": tid},
        )
    )[0]
    return store.open_verified(ring, tid, bytes(row[0]), bytes(row[1]), row[2])


async def test_the_owner_downloads_the_exact_bytes_once(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, ring = await _ready(w, worker)
        r = await mint(w, s.session_id, sub=s.sub)
        assert r.status_code == 201, r.text
        assert r.headers["cache-control"] == "no-store"
        token, ttl = r.json()["token"], r.json()["expires_in"]
        assert 0 < ttl <= 120
        got = await download(w, s.session_id, token)
        assert got.status_code == 200, got.text
        body = got.content
        assert body == await stored(pg, ring, s.session_id)
        h = (
            await pg.run(
                "super",
                "SELECT content_hash, filename FROM assess_transcripts WHERE id = :t",
                {"t": s.session_id},
            )
        )[0]
        assert hashlib.sha256(body).hexdigest() == h[0]
        assert got.headers["content-type"] == "text/html; charset=utf-8"
        assert got.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["referrer-policy"] == "no-referrer"
        cd = got.headers["content-disposition"]
        assert cd.startswith("attachment; ") and f'filename="{h[1]}"' in cd
        assert naming.PATTERN.match(h[1]) and h[1].startswith("EA-2026-27_")
        # single use: the same token again is refused
        again = await download(w, s.session_id, token)
        assert again.status_code == 401 and err(again)["code"] == "ERR-ASSESS-018"
        for event in ("assess_download_token_mint", "assess_student_download"):
            assert (
                await count(
                    pg,
                    "SELECT count(*) FROM assess_audit_events WHERE event = :e AND session_id = :s",
                    e=event,
                    s=s.session_id,
                )
                == 1
            )


async def test_student_b_cannot_get_a_token_for_a_transcript(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        r = await mint(w, s.session_id, sub=s.sub + 1)
        assert r.status_code == 403
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = "
                "'assess_ownership_mismatch_rejected' AND session_id = :s",
                s=s.session_id,
            )
            == 1
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcript_download_tokens WHERE transcript_id = :t",
                t=s.session_id,
            )
            == 0
        )


async def test_expired_foreign_and_query_tokens_are_refused(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        token = (await mint(w, s.session_id, sub=s.sub)).json()["token"]
        # expired, on the database clock (the row decides; the JWT is still within its exp)
        await pg.run(
            "super",
            "UPDATE assess_transcript_download_tokens SET expires_at = now() - interval '1 second'",
        )
        r = await download(w, s.session_id, token)
        assert r.status_code == 401 and err(r)["code"] == "ERR-ASSESS-018"
        # a token for another transcript id
        token = (await mint(w, s.session_id, sub=s.sub)).json()["token"]
        other = "00000000-0000-4000-8000-000000000001"
        assert (await download(w, other, token)).status_code == 401
        # the token in the query string only
        q = await w.client.get(
            f"/api/v1/assess/transcripts/{s.session_id}?token={token}"
        )
        assert q.status_code == 401
        # the discussion (session) token is never a download token
        d = await download(w, s.session_id, s.session_token)
        assert d.status_code == 401
        # the right token still works once (none of the refusals consumed it)
        assert (await download(w, s.session_id, token)).status_code == 200


async def test_a_grader_downloads_the_same_bytes_and_a_non_grader_is_refused(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        student = (
            await download(
                w,
                s.session_id,
                (await mint(w, s.session_id, sub=s.sub)).json()["token"],
            )
        ).content
        # stale grader facts: refused, and a refresh is queued for the retry
        await _graders(w, s, age="1 hour")
        r = await mint(w, s.session_id, key="teacher", actor_userid=12)
        assert r.status_code == 403 and err(r)["code"] == "ERR-ASSESS-021"
        assert err(r)["details"]["reason"] == "grader_check_stale"
        assert "queued" in err(r)["remediation"]
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'render_check' "
                "AND (args->>'student_userid')::bigint = :u AND state = 'ready'",
                u=s.sub,
            )
            >= 1
        )
        await _graders(w, s)
        r = await mint(w, s.session_id, key="teacher", actor_userid=12)
        assert r.status_code == 201, r.text
        teacher = await download(w, s.session_id, r.json()["token"])
        assert teacher.status_code == 200
        assert teacher.content == student  # copies byte-identical (REQ-088)
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_teacher_download' "
                "AND session_id = :s",
                s=s.session_id,
            )
            == 1
        )
        # not a grader of this activity
        r = await mint(w, s.session_id, key="teacher", actor_userid=99)
        assert r.status_code == 403 and err(r)["code"] == "ERR-ASSESS-021"
        assert err(r)["details"]["reason"] == "not_a_grader"
        assert "queued" not in err(r)["remediation"]
        # a grader who lost the capability between mint and download
        token = (await mint(w, s.session_id, key="teacher", actor_userid=13)).json()[
            "token"
        ]
        await _graders(w, s, ids=(12,))
        assert (await download(w, s.session_id, token)).status_code == 403


async def test_keys_are_exact_and_site_bound(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        r = await mint(w, s.session_id, key="admin", sub=s.sub)
        assert r.status_code == 403  # the admin superscope does not inherit assess:*
        r = await mint(w, s.session_id, key="mint_other", sub=s.sub)
        assert (
            r.status_code == 404
        )  # another site's key never learns the transcript exists
        r = await mint(w, s.session_id, key="mint", actor_userid=12)
        assert r.status_code == 400  # a mint key cannot act as a grader
        r = await mint(w, s.session_id, sub=s.sub, extra="x")
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014"


async def test_resend_never_takes_a_recipient(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await _graders(w, s)
        url = f"/api/v1/assess/transcripts/{s.session_id}/resend"
        for body in (
            {"actor_userid": 12, "recipient": "x@evil.example"},
            {"actor_userid": 12, "to": "x@evil.example"},
            {"actor_userid": 12, "options": {"email": "x@evil.example"}},
        ):
            r = await w.client.post(url, json=body, headers=auth("teacher"))
            assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-019", body
        for q in ("recipient=x@evil.example", "email=x", "bcc=x"):
            r = await w.client.post(
                f"{url}?{q}", json={"actor_userid": 12}, headers=auth("teacher")
            )
            assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-019", q
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t AND round > 1",
                t=s.session_id,
            )
            == 0
        )
        # round 1 was just created by the generation: too soon (review #6), nothing written
        r = await w.client.post(url, json={"actor_userid": 12}, headers=auth("teacher"))
        assert r.status_code == 429 and err(r)["details"]["reason"] == "resend_interval"
        assert int(r.headers["retry-after"]) == 600
        # a grader's resend: round 2 for the same recipients; an operator's: round 3
        await _age_rounds(pg, s)
        r = await w.client.post(url, json={"actor_userid": 12}, headers=auth("teacher"))
        assert r.status_code == 202 and r.json()["round"] == 2, r.text
        await _age_rounds(pg, s)
        r = await w.client.post(url, json={}, headers=auth("admin"))
        assert r.status_code == 202 and r.json()["round"] == 3, r.text
        rows = await pg.run(
            "super",
            "SELECT round, recipient_ref FROM assess_deliveries WHERE transcript_id = :t",
            {"t": s.session_id},
        )
        # the same configured recipients in every round, by ref (never an address)
        for n in (1, 2, 3):
            assert {x.recipient_ref for x in rows if x.round == n} == refs(w.site)
        assert len(rows) == 6
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_jobs WHERE kind = 'transcript_deliver' "
                "AND args->>'transcript_id' = :t",
                t=str(s.session_id),
            )
            == 3
        )
        # a non-grader teacher cannot resend
        r = await w.client.post(url, json={"actor_userid": 99}, headers=auth("teacher"))
        assert r.status_code == 403


async def test_an_altered_stored_transcript_is_not_served(pg, worker) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await pg.run(
            "super",
            "UPDATE assess_transcripts SET content_hash = repeat('0', 64) WHERE id = :t",
            {"t": s.session_id},
        )
        token = (await mint(w, s.session_id, sub=s.sub)).json()["token"]
        r = await download(w, s.session_id, token)
        assert r.status_code == 500 and err(r)["code"] == "ERR-ASSESS-020"
        assert b"<html" not in r.content


async def test_resend_bodies_are_capped_and_never_a_500(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review #5: the resend reads its body through the same streamed cap as every route, and
    a nesting deep enough to exhaust the JSON decoder is a 400 envelope."""
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await _graders(w, s)
        url = f"/api/v1/assess/transcripts/{s.session_id}/resend"
        big = b'{"actor_userid": 12, "pad": "' + b"x" * 70_000 + b'"}'
        deep = b'{"a":' * 100_000 + b"1" + b"}" * 100_000
        for raw in (big, deep, b"[" * 50_000):
            r = await w.client.post(
                url,
                content=raw,
                headers={**auth("teacher"), "Content-Type": "application/json"},
            )
            assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014", (
                r.status_code
            )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t AND round > 1",
                t=s.session_id,
            )
            == 0
        )


async def test_resend_is_rate_limited_per_key(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Review #6: at most VEKTRA_ASSESS_RESEND_RPM resend calls per minute per API key."""
    from support.world import default_settings

    async with world(pg, default_settings(VEKTRA_ASSESS_RESEND_RPM=2)) as w:
        s, _ = await _ready(w, worker)
        url = f"/api/v1/assess/transcripts/{s.session_id}/resend"
        responses = [
            await w.client.post(url, json={}, headers=auth("admin")) for _ in range(3)
        ]
        # the first two pass the per-key limiter and meet the interval (round 1 is fresh);
        # the third is the per-key limiter's own refusal
        assert [(r.status_code, err(r)["details"]["reason"]) for r in responses] == [
            (429, "resend_interval"),
            (429, "resend_interval"),
            (429, "resend_rate"),
        ]


async def test_a_stale_grader_list_at_download_does_not_burn_the_token(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """Review #7: facts fresh at mint stay acceptable for the token's lifetime; beyond that the
    GET is refused WITHOUT consuming the jti, so the same token works after the refresh."""
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await _graders(w, s)
        token = (await mint(w, s.session_id, key="teacher", actor_userid=12)).json()[
            "token"
        ]
        # older than the bound (120 s) but within bound + TTL (60 s): still served
        await _graders(w, s, age="150 seconds")
        assert (await download(w, s.session_id, token)).status_code == 200
        await _graders(w, s)
        token = (await mint(w, s.session_id, key="teacher", actor_userid=12)).json()[
            "token"
        ]
        await _graders(w, s, age="1 hour")
        r = await download(w, s.session_id, token)
        assert r.status_code == 403 and err(r)["code"] == "ERR-ASSESS-021"
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcript_download_tokens WHERE transcript_id = :t "
                "AND consumed_at IS NULL",
                t=s.session_id,
            )
            == 1
        )
        await _graders(w, s)  # the refresh the 403 queued has run
        assert (await download(w, s.session_id, token)).status_code == 200
        assert (await download(w, s.session_id, token)).status_code == 401  # used once


async def test_mint_and_download_share_one_rate_budget(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503025922: with VEKTRA_ASSESS_DOWNLOAD_RPM=2 one mint-and-download pair
    fits, a second mint in the same minute does not."""
    from support.world import default_settings

    async with world(pg, default_settings(VEKTRA_ASSESS_DOWNLOAD_RPM=2)) as w:
        s, _ = await _ready(w, worker)
        r = await mint(w, s.session_id, sub=s.sub)
        assert r.status_code == 201
        assert (await download(w, s.session_id, r.json()["token"])).status_code == 200
        r = await mint(w, s.session_id, sub=s.sub)
        assert r.status_code == 429 and err(r)["details"]["reason"] == "download_rate"


async def test_a_transcript_key_not_loaded_is_503_and_keeps_the_token(
    pg, worker
) -> None:  # type: ignore[no-untyped-def]
    """CodeRabbit 4503029403: during a key rotation the API may not hold the kid yet: 503
    (retryable), the token stays usable, no integrity failure is reported."""
    async with world(pg) as w:
        s, ring = await _ready(w, worker)
        token = (await mint(w, s.session_id, sub=s.sub)).json()["token"]
        w.app.state.assess_keys[store.KEYRING] = AssessKeyring(
            store.KEYRING, "t2", {"t2": os.urandom(32)}
        )
        r = await download(w, s.session_id, token)
        assert r.status_code == 503 and err(r)["code"] == "ERR-ASSESS-001"
        w.app.state.assess_keys[store.KEYRING] = ring  # the key arrives
        assert (await download(w, s.session_id, token)).status_code == 200


def _same_refusal(a, b) -> None:  # type: ignore[no-untyped-def]
    """Two refusals a caller cannot tell apart: status and envelope, bar the request id."""
    assert a.status_code == b.status_code == 404, (a.text, b.text)
    ea = {k: v for k, v in err(a).items() if k != "request_id"}
    eb = {k: v for k, v in err(b).items() if k != "request_id"}
    assert ea == eb and ea["code"] == "ERR-ASSESS-017"


async def resend(w, tid, key: str = "teacher", **body: Any):  # type: ignore[no-untyped-def]
    return await w.client.post(
        f"/api/v1/assess/transcripts/{tid}/resend", json=body, headers=auth(key)
    )


async def test_the_page_cmid_scopes_the_grader_routes(pg, worker) -> None:  # type: ignore[no-untyped-def]
    """Plugin request: the plugin checks the capability on its PAGE's cmid, Vektra on the
    transcript's activity. With ``cmid`` in the body, a transcript of another activity is
    refused exactly as an unknown transcript (404 ERR-ASSESS-017, same envelope), audited;
    the same activity passes; absent ``cmid`` is unchanged. Both grader routes, both keys."""
    import uuid

    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await _graders(
            w, s
        )  # 12 grades the transcript's activity: only the cmid can refuse
        other = s.cmid + 1
        unknown = uuid.uuid4()
        audit_sql = (
            "SELECT count(*) FROM assess_audit_events WHERE event = "
            "'assess_ownership_mismatch_rejected' AND session_id = :s AND reason_code = :r"
        )

        # download-token, grader path
        same = await mint(w, s.session_id, key="teacher", actor_userid=12, cmid=s.cmid)
        assert same.status_code == 201, same.text
        absent = await mint(w, s.session_id, key="teacher", actor_userid=12)
        assert absent.status_code == 201, absent.text
        tokens_before = await count(
            pg,
            "SELECT count(*) FROM assess_transcript_download_tokens WHERE transcript_id = :t",
            t=s.session_id,
        )
        cross = await mint(w, s.session_id, key="teacher", actor_userid=12, cmid=other)
        _same_refusal(
            cross, await mint(w, unknown, key="teacher", actor_userid=12, cmid=other)
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_transcript_download_tokens "
                "WHERE transcript_id = :t",
                t=s.session_id,
            )
            == tokens_before
        )
        assert (
            await count(
                pg, audit_sql, s=s.session_id, r="transcript_download_token_cmid"
            )
            == 1
        )
        # the owner path takes it too, with the same refusal
        assert (await mint(w, s.session_id, sub=s.sub, cmid=s.cmid)).status_code == 201
        _same_refusal(
            await mint(w, s.session_id, sub=s.sub, cmid=other),
            await mint(w, unknown, sub=s.sub, cmid=other),
        )

        # resend, grader and operator
        cross = await resend(w, s.session_id, actor_userid=12, cmid=other)
        _same_refusal(cross, await resend(w, unknown, actor_userid=12, cmid=other))
        _same_refusal(
            await resend(w, s.session_id, key="admin", cmid=other),
            await resend(w, unknown, key="admin", cmid=other),
        )
        assert (
            await count(pg, audit_sql, s=s.session_id, r="transcript_resend_cmid") == 2
        )
        assert (
            await count(
                pg,
                "SELECT count(*) FROM assess_deliveries WHERE transcript_id = :t "
                "AND round > 1",
                t=s.session_id,
            )
            == 0
        )
        await _age_rounds(pg, s)
        r = await resend(w, s.session_id, actor_userid=12, cmid=s.cmid)
        assert r.status_code == 202 and r.json()["round"] == 2, r.text
        await _age_rounds(pg, s)
        r = await resend(w, s.session_id, actor_userid=12)  # absent: unchanged
        assert r.status_code == 202 and r.json()["round"] == 3, r.text


@pytest.mark.parametrize("bad", [0, -1, "501", 1.5, True, 2**53])
async def test_an_invalid_page_cmid_is_a_400(pg, worker, bad) -> None:  # type: ignore[no-untyped-def]
    async with world(pg) as w:
        s, _ = await _ready(w, worker)
        await _graders(w, s)
        r = await mint(w, s.session_id, key="teacher", actor_userid=12, cmid=bad)
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014", r.text
        reason = err(r)["details"]["reason"]
        assert reason.endswith(":cmid") and not reason.startswith("extra_forbidden")
        r = await resend(w, s.session_id, actor_userid=12, cmid=bad)
        assert r.status_code == 400 and err(r)["code"] == "ERR-ASSESS-014", r.text
        reason = err(r)["details"]["reason"]
        assert reason.endswith(":cmid") and not reason.startswith("extra_forbidden")
