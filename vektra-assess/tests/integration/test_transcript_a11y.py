"""NFR-016 / BR-016: rendered transcripts pass axe-core (WCAG 2.1 AA) and the Nu HTML checker.

Two rendered transcripts (a quiz and an assignment with criteria, files, math, every marker and a
"Punti contestati" list) are scanned in Chromium with axe-core and validated by the Nu HTML
Checker (``ghcr.io/validator/validator``, run with ``docker``). Evidence: 0 axe violations, 0
checker errors, ``<html lang="it">`` and UTF-8. A manual NVDA read-through is outside CI (plan
Notes). Needs the UI's node_modules (playwright-core, axe-core), a Playwright Chromium and Docker:
SKIP locally without them, FAIL in CI (``VEKTRA_ASSESS_E2E_REQUIRED=1``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_context_snapshot import make as make_quiz

from vektra_assess.context.regrade import RegradeFlag, RegradeKind
from vektra_assess.context.snapshot import (
    ActivityKind,
    AssignCriterion,
    FileEntry,
    SubmissionItem,
)
from vektra_assess.parser_protocol import FileStatus
from vektra_assess.transcript import allowlist, render

pytestmark = pytest.mark.integration

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
REQUIRED = os.environ.get("VEKTRA_ASSESS_E2E_REQUIRED") == "1"
VNU_IMAGE = "ghcr.io/validator/validator:23.4.11"
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)

TURNS = (
    ("bot", "opening", "Benvenuto. Discutiamo la valutazione."),
    (
        "student",
        "normal",
        "Perché \\(\\frac{3}{4}\\) e non $$\\sqrt{x^2+1}$$? <b>grassetto</b>",
    ),
    ("bot", "normal", "Hai indicato C1. [risposta sostituita dal sistema]"),
    ("student", "normal", "I think the rubric was misapplied on C1."),
    ("bot", "interrupted", "[risposta interrotta]"),
)


def _skip(msg: str) -> None:
    pytest.fail(msg) if REQUIRED else pytest.skip(msg)


def _docs() -> dict[str, bytes]:
    quiz = make_quiz()
    assign = make_quiz().model_copy(
        update={
            "header": make_quiz().header.model_copy(
                update={"activity_kind": ActivityKind.ASSIGN, "grade_method": None}
            ),
            "quiz_items": (),
            "criteria": (
                AssignCriterion(
                    label="C1", level_index=2, level_count=4, points="3", max_points="5",
                    remark="Struttura migliorabile.",
                ),
            ),
            "submission": SubmissionItem(onlinetext="testo"),
            "files": (
                FileEntry(label="F1", filename="relazione.pdf", status=FileStatus.READ, text="x"),
                FileEntry(label="F2", filename="scansione.pdf", status=FileStatus.NO_TEXT),
            ),
        }
    )  # fmt: skip
    out = {}
    for name, snap in (("quiz", quiz), ("assign", assign)):
        turns = tuple(
            render.TurnRecord(
                no=i + 1,
                role=role,
                kind=kind,
                text=text,
                at=T0 + timedelta(minutes=i),
                flagged=i == 2,
                marks=("values",) if i == 2 else (),
                contests=(("C1", "confirmed"),) if i == 2 else (),
                lang="en" if i == 3 else ("other" if i == 1 else "it"),
            )
            for i, (role, kind, text) in enumerate(TURNS)
        )
        inp = render.TranscriptInput(
            course="EA-2026-" + "X" * 80, cmid=501, student_userid=101, session_no=2, sessions_total=2,
            earlier=(render.EarlierSession(1, T0 - timedelta(days=3), "timed_out"),),
            previous_context=False, snapshot=snap,
            regrade=RegradeFlag(RegradeKind.WITHDRAWN, old="7.5"), cap_minutes=30,
            duration_s=2700, committed_at=T0, closed_at=T0 + timedelta(minutes=40),
            close_reason="finished",
            outages=(render.Outage(T0 + timedelta(minutes=5), T0 + timedelta(minutes=7), 120),),
            credited_s=120, failed_starts=1, turns=turns, digest="a" * 64,
            generated_at=T0 + timedelta(minutes=41),
        )  # fmt: skip
        doc = render.render(render.build_doc(inp))
        allowlist.check(doc)
        out[name] = doc
    return out


async def _run(
    *cmd: str, cwd: Path | None = None, timeout: float = 180
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode or 0, out.decode(errors="replace")


AXE = r"""
const {chromium} = require('playwright-core');
const fs = require('fs');
const axeSource = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
(async () => {
  const files = JSON.parse(process.argv[1]);
  const browser = await chromium.launch();
  const out = [];
  for (const f of files) {
    const page = await browser.newPage();
    await page.goto('file://' + f);
    await page.addScriptTag({content: axeSource});
    const r = await page.evaluate(() => axe.run(document, {runOnly: {type: 'tag',
      values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}}));
    // WCAG 1.4.10 reflow: at 320 CSS px nothing scrolls horizontally
    await page.setViewportSize({width: 320, height: 640});
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    out.push({file: f, overflow, violations: r.violations.map(v => v.id), passes: r.passes.length,
      lang: await page.evaluate(() => document.documentElement.lang),
      charset: await page.evaluate(() => document.characterSet)});
    await page.close();
  }
  await browser.close();
  console.log('RESULT ' + JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(2); });
"""


async def test_transcripts_pass_axe_and_the_nu_checker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    if not (UI_DIR / "node_modules" / "axe-core").is_dir():
        _skip("vektra-assess/ui/node_modules is missing (npm ci)")
    if shutil.which("docker") is None:
        _skip("docker CLI not available for the Nu HTML checker")
    files = []
    for name, doc in _docs().items():
        path = tmp_path / f"{name}.html"
        path.write_bytes(doc)
        files.append(str(path))
    code, out = await _run("node", "-e", AXE, json.dumps(files), cwd=UI_DIR)
    if code != 0 and "Executable doesn't exist" in out:
        _skip("no Playwright Chromium (npx playwright install chromium)")
    assert code == 0, out
    results = json.loads(out.split("RESULT ", 1)[1])
    assert len(results) == 2, results  # false-pass guard: both documents were scanned
    for r in results:
        assert r["violations"] == [], r
        assert r["overflow"] <= 0, r
        assert r["passes"] > 10 and r["lang"] == "it" and r["charset"] == "UTF-8", r
    # control: a broken document must produce errors, so a silent checker cannot pass
    (tmp_path / "control.html").write_text(
        '<!DOCTYPE html><html lang="it"><head></head><body><dl><p>x</p></dl></body></html>'
    )
    code, out = await _run(
        "docker", "run", "--rm", "-v", f"{tmp_path}:/data:ro", VNU_IMAGE,
        "vnu", "--format", "json", "/data/quiz.html", "/data/assign.html",
        "/data/control.html", timeout=300,
    )  # fmt: skip
    report = json.loads(out[out.index("{") :])
    errors = [m for m in report["messages"] if m.get("type") == "error"]
    by_file = {Path(m.get("url", "")).name for m in errors}
    assert by_file == {"control.html"}, errors
