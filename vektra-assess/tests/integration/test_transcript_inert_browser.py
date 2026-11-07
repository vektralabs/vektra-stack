"""BR-016 in a browser: transcripts built from hostile turns are inert (plan 10).

Hostile content (XSS, an event handler, a markdown image, a remote URL and a link, header-like
text, an SVG handler next to math) is posted through the real turn route and answered by the fake
model with the same text; the generated transcript is decrypted and opened in Chromium from
``file://`` with network logging: 0 requests, 0 script elements, 0 executed payloads, 0 dialogs.
Needs the UI's node_modules and a Playwright Chromium: the assess-ui-e2e CI job runs it with
``VEKTRA_ASSESS_E2E_REQUIRED=1`` (a skip is then a failure).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakeStreamRunner
from support.sessions import seed_session
from support.world import world

from integration.test_session_events_sse import TIMING
from integration.test_session_preflight import _reset
from integration.test_transcript_generation import Facts, _open, transcript_ring
from vektra_assess.llm.types import ContestEntry
from vektra_assess.transcript import allowlist
from vektra_assess.transcript.facts import CloseFacts
from vektra_assess.worker.transcript_gen import GenerationDeps, generate_transcript

pytestmark = pytest.mark.integration

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
REQUIRED = os.environ.get("VEKTRA_ASSESS_E2E_REQUIRED") == "1"
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
