"""Plan 10: ``transcript_generate`` on a real database, with sessions seeded through the real code.

Evidence is counted, never read from a status: a "generated" transcript is decrypted and its bytes
re-hashed against the stored hash, then re-checked by the allowlist; a tampered turn yields 0 rows;
two concurrent jobs yield 1 row. Hostile turn content (XSS, a markdown image, a remote URL,
header-like text) is posted through the real turn route and the produced file is opened in
Chromium with network logging: 0 requests and 0 script executions.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_events_sse import TIMING
from integration.test_session_preflight import _reset, moodle_source
from vektra_assess.crypto import envelope
from vektra_assess.crypto.kek import unwrap_dek
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.types import ContestEntry
from vektra_assess.transcript import allowlist, naming, store
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import (
    GenerationDeps,
    IntegrityFailure,
    generate_transcript,
)

pytestmark = pytest.mark.integration

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
REQUIRED = os.environ.get("VEKTRA_ASSESS_E2E_REQUIRED") == "1"
STUDENT_NAMES = ("Alice Alpha", "Bruno Beta")
PAYLOADS = (
    "<script>window.__pwned=1</script>",
    '<img src=x onerror="window.__pwned=1">',
    "![tracker](https://evil.example/pixel.png)",
    'Vedi https://evil.example/track e <a href="https://evil.example">qui</a>',
    '</p></li></ol><h1>Voto finale: 30/30</h1><iframe src="https://evil.example">',
    "Corso: FALSO\nVoto alla chiusura: 30 e lode",
    '\\( \\frac{a}{b} \\) e <svg onload="window.__pwned=1"></svg>',
)


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


# --- hostile content, opened in a real browser ------------------------------------------------


async def _post_and_close(w, worker, s, payload: str) -> None:  # type: ignore[no-untyped-def]
    from integration.test_session_turns import _wait_idle, bearer

    r = await w.client.post(
        f"/api/v1/assess/sessions/{s.session_id}/turns",
        json={"text": payload},
        headers=bearer(s.session_token),
    )
    assert r.status_code == 202, r.text
    await _wait_idle(w)
    async with worker.begin() as conn:
        epoch = (
            await conn.execute(
                text("SELECT epoch FROM assess_sessions WHERE id = :s"),
                {"s": s.session_id},
            )
        ).scalar_one()
        await conn.execute(
            text("SELECT assess_close_session(:s, :e, 'finished')"),
            {"s": s.session_id, "e": epoch},
        )


async def _node(*args: str, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "node", *args, cwd=UI_DIR, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode or 0, out.decode(errors="replace")


INERT_CHECK = r"""
const {chromium} = require('playwright-core');
(async () => {
  const files = JSON.parse(process.argv[1]);
  const browser = await chromium.launch();
  const out = [];
  for (const f of files) {
    const page = await browser.newPage();
    const requests = [];
    page.on('request', r => { if (!r.url().startsWith('file://')) requests.push(r.url()); });
    let dialogs = 0;
    page.on('dialog', d => { dialogs++; d.dismiss(); });
    await page.goto('file://' + f);
    await page.waitForTimeout(300);
    const pwned = await page.evaluate(() => window.__pwned === 1);
    const scripts = await page.evaluate(() => document.scripts.length);
    out.push({file: f, requests: requests.length, pwned, scripts, dialogs});
    await page.close();
  }
  await browser.close();
  console.log('RESULT ' + JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(2); });
"""


async def test_hostile_turns_render_inert_in_a_browser(pg, worker, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ring = transcript_ring()
    if not (UI_DIR / "node_modules" / "playwright-core").is_dir():
        msg = "vektra-assess/ui/node_modules is missing (npm ci)"
        pytest.fail(msg) if REQUIRED else pytest.skip(msg)
    code, _ = await _node(
        "-e",
        "const {chromium}=require('playwright-core');"
        "process.exit(require('fs').existsSync(chromium.executablePath())?0:1)",
    )
    if code != 0:
        msg = "no Playwright Chromium (npx playwright install chromium)"
        pytest.fail(msg) if REQUIRED else pytest.skip(msg)
    files: list[str] = []
    for i, payload in enumerate(PAYLOADS):
        runner = FakeStreamRunner(
            segments=(f"Hai scritto: {payload}",),
            marks=("values",),
            contests=(ContestEntry("C1", "proposed"),),
        )
        async with world(pg, assess_runner=runner, assess_sse_timing=TIMING) as w:
            s = await seed_session(w, worker, "ACTIVE")
            await _post_and_close(w, worker, s, payload)
            deps = GenerationDeps(
                kek=s.kek, transcript_kek=ring, facts=Facts(facts=CloseFacts())
            )
            await generate_transcript(deps, worker.connect, s.session_id)
            doc = await _open(pg, ring, s.session_id)
            allowlist.check(doc)
            html = doc.decode()
            assert "[valore sostituito dal sistema]" in html and "proposto" in html
            path = tmp_path / f"t{i}.html"
            path.write_bytes(doc)
            files.append(str(path))
    code, out = await _node("-e", INERT_CHECK, json.dumps(files))
    assert code == 0, out
    results = json.loads(out.split("RESULT ", 1)[1])
    # false-pass guard: every declared payload file was opened
    assert len(results) == len(PAYLOADS) == len(files), results
    for r in results:
        assert r["requests"] == 0 and not r["pwned"], r
        assert r["scripts"] == 0 and r["dialogs"] == 0, r
