"""Plan 9 e2e: the discussion UI in a real browser, framed across two origins (Playwright + axe).

The harness serves, in this test's event loop:

* the assess API and the UI page (the production routers over the migrated Postgres, as the
  restricted API role) on ``http://localhost:<p2>``;
* a Moodle stub on ``http://127.0.0.1:<p1>``: the host page that frames the UI with the plugin's
  sandbox attributes and plays the parent side of the postMessage contract (server-side mint of
  the pre-session token, no ``sid`` on a resume, no answer to a download-token request: the
  download part is plan 10's), plus ``/control/*`` endpoints the specs use to drive the fakes and
  to read server state;
* a third origin, ``http://localhost:<p1>`` (same stub, another host name), that must not be able
  to frame the UI.

The model is fake (``Runner`` below: scripted replies, an outage switch, a hold after the first
segment); the pre-flight is the production handler with the recorded Moodle 4.3 responses, run by
a background loop like the worker's; the outage probe is the worker's ``outage_tick`` with a probe
that follows the outage switch; pending turns are answered by the API's ``PendingTurnDriver``.

The specs live in ``vektra-assess/ui/e2e/`` and run with ``npx playwright test``. This test
passes only when every spec passed and the download spec ended SKIPPED as BLOCKED (plan 10):
a download that passed would prove an endpoint that does not exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from assess_moodle_support import Replay, make_client, manifest
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from support.preflight import FakePartB, activate, deps, job_context, kek_ring
from support.sessions import fixture_target
from support.world import beat, configure_activity, default_settings, world
from test_context_capture import InProcessParser

from integration.test_session_preflight import _reset
from vektra_assess.api.pending import PendingTurnDriver
from vektra_assess.llm.types import (
    ProviderCallRecord,
    ProviderOutageError,
    ReleasedSegment,
    TurnResult,
    TurnUsage,
)
from vektra_assess.worker.outage_probe import outage_tick
from vektra_assess.worker.preflight import make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

pytestmark = pytest.mark.integration

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
BRANCH = "4.3"
#: CI sets it: a missing Node toolchain or browser is then a failure, never a skip
REQUIRED = os.environ.get("VEKTRA_ASSESS_E2E_REQUIRED") == "1"
#: the spec that must end skipped as BLOCKED until plan 10 ships the download endpoint
BLOCKED_SPEC = "download returns the stored transcript bytes"
#: the specs that must run and pass (the e2e/*.e2e.ts files; the BLOCKED one excluded)
SPECS_RUN = 7


def _toolchain_problem() -> str | None:
    if not (UI_DIR / "node_modules" / "@playwright" / "test").is_dir():
        return "vektra-assess/ui/node_modules is missing (npm ci)"
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Runner:
    """``run_stream`` without a model. ``down``: every call is a provider outage; ``hold``: the
    reply stops after its first released segment until ``release()`` (the streaming state)."""

    def __init__(self) -> None:
        self.down = False
        self.hold = False
        self._release = asyncio.Event()
        self.calls = 0

    def release(self) -> None:
        self.hold = False
        self._release.set()

    async def run_stream(self, bundle: Any, turn: Any, stop: Any = None) -> Any:
        self.calls += 1
        n = self.calls
        if self.down:
            now = datetime.now(UTC)
            raise ProviderOutageError(
                ProviderCallRecord(now, None, now, "first_token_timeout")
            )
        segments = (f"Risposta {n}: ", "parliamo del criterio C1.")
        yield ReleasedSegment(segments[0])
        if self.hold:
            self._release.clear()
            await self._release.wait()
        yield ReleasedSegment(segments[1])
        body = "".join(segments)
        now = datetime.now(UTC)
        yield TurnResult(
            interrupted=False,
            text=body,
            marks=(),
            behaviour_class=None,
            contest_entries=(),
            language="it",
            usage=TurnUsage(200, 20),
            call_records=(ProviderCallRecord(now, now, now, "ok"),),
            replaced_count=0,
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
        )


class Harness:
    def __init__(self, w: Any, worker: Any, runner: Runner, ports: dict[str, int]):
        self.w = w
        self.worker = worker
        self.runner = runner
        self.paused = False
        self.done: set[uuid.UUID] = set()
        self.host = f"http://127.0.0.1:{ports['stub']}"
        self.third = f"http://localhost:{ports['stub']}"
        self.ui_origin = f"http://localhost:{ports['ui']}"
        source = MoodleStartSource(
            make_client(Replay(BRANCH)), InProcessParser(), worker.connect
        )
        self.preflight = make_preflight_handler(
            deps(w.settings, w.handoff_public, source, FakePartB(), kek=kek_ring())
        )

    # --- the worker's side: pre-flight jobs and the outage probe -------------------------------

    async def worker_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover - surfaced by the specs failing
                print("e2e worker tick failed:", type(exc).__name__, exc)
            await asyncio.sleep(0.3)

    async def _tick(self) -> None:
        await beat(self.w.pg)  # the API refuses a Start while the worker looks dead
        if not self.paused:
            rows = await self.w.pg.run(
                "super", "SELECT id FROM assess_sessions WHERE state = 'STARTING'"
            )
            for (sid,) in rows:
                if sid not in self.done:
                    self.done.add(sid)
                    await self.preflight(job_context(self.worker, sid))
                    if os.environ.get("E2E_LOG") == "info":
                        print(
                            "e2e preflight",
                            sid,
                            await self.w.pg.run(
                                "super",
                                "SELECT outcome, reason_code FROM assess_start_attempts "
                                "WHERE session_id = :s",
                                {"s": sid},
                            ),
                        )

        async def probe() -> bool:
            return not self.runner.down

        await outage_tick(self.worker.connect, probe, min_credit_s=1)

    # --- the stub's helpers ---------------------------------------------------------------------

    def target(self, request: Request) -> tuple[int, int]:
        q = request.query_params
        return fixture_target(BRANCH, q.get("a", "rubric_vis"), q.get("who", "stu1"))

    async def mint(self, request: Request) -> Response:
        sub, cmid = self.target(request)
        await configure_activity(self.w.pg, self.w.site, cmid, cap_minutes=30)
        r = await self.w.mint(sub, cmid)
        if r.status_code != 201:
            return JSONResponse({"error": r.text}, status_code=502)
        return JSONResponse({"token": r.json()["token"]})

    def host_page(self, request: Request) -> Response:
        q = request.query_params
        cfg = {
            "uiOrigin": self.ui_origin,
            "ui": f"{self.ui_origin}/api/v1/assess/ui?site={self.w.site}",
            "mode": q.get("mode", "start"),
            "sid": q.get("sid"),
            "hold": q.get("hold") == "1",
            "unavailable": q.get("unavailable") == "1",
            "mint": f"/mint?who={q.get('who', 'stu1')}&a={q.get('a', 'rubric_vis')}",
            "evil": f"{self.third}/evil?who={q.get('who', 'stu1')}"
            f"&a={q.get('a', 'rubric_vis')}"
            if q.get("evil") == "1"
            else None,
        }
        return HTMLResponse(HOST_PAGE.replace("__CFG__", json.dumps(cfg)))

    def evil_page(self, request: Request) -> Response:
        q = request.query_params
        mint = f"/mint?who={q.get('who', 'stu1')}&a={q.get('a', 'rubric_vis')}"
        return HTMLResponse(EVIL_PAGE.replace("__MINT__", json.dumps(mint)))

    async def session(self, request: Request) -> Response:
        """Server state of the target's latest session; the turns come from the API itself."""
        sub, cmid = self.target(request)
        rows = await self.w.pg.run(
            "super",
            "SELECT id, state, close_reason, deadline_at, (SELECT count(*) FROM assess_jobs j "
            "WHERE j.kind = 'transcript_generate' AND j.args->>'session_id' = s.id::text) "
            "FROM assess_sessions s WHERE site_id = :site AND student_userid = :u "
            "AND cmid = :c ORDER BY session_no DESC LIMIT 1",
            {"site": self.w.site, "u": sub, "c": cmid},
        )
        if not rows:
            return JSONResponse({"session_id": None})
        sid, state, reason, deadline_at, jobs = rows[0]
        turns: list[dict[str, Any]] = []
        if state != "CLOSED":
            token = await self.w.presession(sub, cmid)
            r = await self.w.client.get(
                f"/api/v1/assess/sessions/{sid}",
                headers={"Authorization": f"Bearer {token}"},
            )
            turns = r.json().get("turns", []) if r.status_code == 200 else []
        return JSONResponse(
            {
                "session_id": str(sid),
                "state": state,
                "close_reason": reason,
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "transcript_jobs": jobs,
                "turns": turns,
            }
        )

    async def control(self, request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json() if await request.body() else {}
        if name == "pause":
            self.paused = bool(body["on"])
        elif name == "outage":
            self.runner.down = bool(body["on"])
        elif name == "hold":
            if body["on"]:
                self.runner.hold = True
            else:
                self.runner.release()
        elif name == "warn":
            # the session's deadline moves to 5 min + 2 s from now (committed_at with it, so the
            # deadline formula still holds): the event stream's own poll then emits the 5-minute
            # warning (REQ-069) through the production path
            sub, cmid = self.target(request)
            await self.w.pg.run(
                "super",
                "UPDATE assess_sessions SET "
                "committed_at = committed_at + (now() + interval '302 s' - deadline_at), "
                "deadline_at = now() + interval '302 s' "
                "WHERE site_id = :site AND student_userid = :u AND cmid = :c "
                "AND state = 'ACTIVE'",
                {"site": self.w.site, "u": sub, "c": cmid},
            )
        else:
            return JSONResponse({"error": name}, status_code=404)
        return JSONResponse({"ok": True})


HOST_PAGE = """<!doctype html>
<html lang="it"><head><meta charset="utf-8"><title>Attività (Moodle di prova)</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0"><main>
<h1 style="font-size:1rem;margin:0.5rem 1rem">Attività</h1>
<iframe id="ui" title="Discussione della valutazione"
  sandbox="allow-scripts allow-same-origin allow-downloads" allow="clipboard-write"
  style="display:block;width:100%;border:0;height:480px"></iframe>
</main>
<script>
const cfg = __CFG__;
const frame = document.getElementById("ui");
window.__fromUi = [];
function post(m) { frame.contentWindow.postMessage(m, cfg.uiOrigin); }
window.addEventListener("message", async (ev) => {
  if (ev.origin !== cfg.uiOrigin || ev.source !== frame.contentWindow) return;
  const m = ev.data;
  window.__fromUi.push(m.type);
  if (m.type === "vektra-assess-resize") frame.style.height = (m.height + 32) + "px";
  if (m.type !== "vektra-assess-ready" || cfg.hold) return;
  if (cfg.unavailable) return post({ v: 1, type: "vektra-assess-unavailable" });
  const r = await fetch(cfg.mint);
  if (!r.ok) return post({ v: 1, type: "vektra-assess-unavailable" });
  const msg = { v: 1, type: "vektra-assess-token", token: (await r.json()).token, mode: cfg.mode };
  if (cfg.sid) msg.sid = cfg.sid;
  post(msg);
  // vektra-assess-download-token-request is not answered: the download part is plan 10's
});
frame.src = cfg.ui;
if (cfg.evil) {
  const evil = document.createElement("iframe");
  evil.title = "altro sito";
  evil.src = cfg.evil;
  document.body.append(evil);
}
</script></body></html>
"""

#: a frame on the third origin, inside the right host page: it posts a VALID pre-session token to
#: its sibling (the UI). The UI must ignore it (wrong origin, wrong source) and make no request.
EVIL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>other</title></head><body>
<script>
(async () => {
  const token = (await (await fetch(__MINT__)).json()).token;
  const msg = { v: 1, type: "vektra-assess-token", token, mode: "start" };
  for (let i = 0; i < 10; i++) {
    parent.frames[0].postMessage(msg, "*");
    await new Promise((r) => setTimeout(r, 200));
  }
  document.title = "posted";
})();
</script></body></html>
"""


@contextlib.asynccontextmanager
async def serving(app: Any, host: str, port: int) -> Any:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=os.environ.get("E2E_LOG", "warning"),
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, f"server on {host}:{port} did not start"
    try:
        yield server
    finally:
        server.should_exit = True
        server.force_exit = True  # open event streams do not hold the shutdown
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)


async def _run(
    *cmd: str, env: dict[str, str] | None = None, timeout: float
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=UI_DIR,
        env={**os.environ, **(env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


@pytest.fixture
async def worker(pg):  # type: ignore[no-untyped-def]
    await _reset(pg)
    engine = create_async_engine(pg.url("worker"))
    yield engine
    await engine.dispose()


def _outcomes(report: dict[str, Any]) -> dict[str, str]:
    """``title -> status`` of every spec in a Playwright JSON report."""
    out: dict[str, str] = {}

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for t in spec.get("tests", []):
                results = t.get("results") or [{}]
                out[spec["title"]] = results[-1].get("status", t.get("status", "?"))
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)
    return out


async def test_the_discussion_ui_in_a_browser_across_two_origins(
    pg, worker, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    problem = _toolchain_problem()
    if problem:
        if REQUIRED:
            pytest.fail(problem)
        pytest.skip(problem)
    code, out = await _run("npm", "run", "build", timeout=300)
    assert code == 0, out
    code, out = await _run(
        "node",
        "-e",
        "const {chromium}=require('playwright-core');"
        "process.exit(require('fs').existsSync(chromium.executablePath())?0:1)",
        timeout=60,
    )
    if code != 0:
        msg = "no Playwright Chromium (npx playwright install chromium)"
        if REQUIRED:
            pytest.fail(msg)
        pytest.skip(msg)

    ports = {"stub": _free_port(), "ui": _free_port()}
    runner = Runner()
    async with (
        world(
            pg,
            # the page answers on VEKTRA_ASSESS_UI_ORIGIN only (review #1)
            default_settings(
                VEKTRA_ASSESS_UI_ORIGIN=f"http://localhost:{ports['ui']}",
                VEKTRA_ASSESS_UI_PAGE_RPM=600,  # one browser reloads the page many times a minute
            ),
            assess_runner=runner,
            assess_sse_timing={"poll_s": 0.3, "keepalive_s": 5.0},
        ) as w
    ):
        await w.pg.run(
            "super",
            "UPDATE assess_sites SET release = :r, moodle_origin = :o WHERE id = :s",
            {
                "r": manifest(BRANCH)["release"],
                "o": f"http://127.0.0.1:{ports['stub']}",
                "s": w.site,
            },
        )
        await activate(pg)
        pending = PendingTurnDriver(w.app, interval_s=0.5, min_age_s=1.0)
        w.app.state.assess_pending_driver = pending
        pending.start()
        h = Harness(w, worker, runner, ports)
        stub = Starlette(
            routes=[
                Route("/host", h.host_page),
                Route("/evil", h.evil_page),
                Route("/mint", h.mint),
                Route("/control/session", h.session),
                Route("/control/{name}", h.control, methods=["POST"]),
            ]
        )
        loop_task = asyncio.create_task(h.worker_loop())
        report = tmp_path / "report.json"
        try:
            async with (
                serving(w.app, "127.0.0.1", ports["ui"]),
                serving(stub, "127.0.0.1", ports["stub"]),
            ):
                grep = os.environ.get("E2E_GREP")  # local debugging of one spec only
                code, out = await _run(
                    "npx",
                    "playwright",
                    "test",
                    "--config",
                    "e2e/playwright.config.ts",
                    *(["--grep", grep] if grep else []),
                    env={
                        "E2E_HOST": h.host,
                        "E2E_THIRD": h.third,
                        "E2E_UI": h.ui_origin,
                        "E2E_REPORT": str(report),
                        "E2E_OUTPUT": str(tmp_path / "results"),
                    },
                    timeout=900,
                )
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            await pending.stop()
            driver = getattr(w.app.state, "assess_turn_driver", None)
            replies = list(getattr(driver, "tasks", {}).values())
            for task in replies:
                task.cancel()
            await asyncio.gather(*replies, return_exceptions=True)
            await (
                worker.dispose()
            )  # its connections belong to this loop, not the fixture's
    print(out)
    assert report.exists(), out
    outcomes = _outcomes(json.loads(report.read_text()))
    assert code == 0, out
    assert outcomes.get(BLOCKED_SPEC) == "skipped", (
        f"the download spec must end SKIPPED (BLOCKED, plan 10): {outcomes}"
    )
    ran = {t: s for t, s in outcomes.items() if t != BLOCKED_SPEC}
    assert ran and all(s == "passed" for s in ran.values()), outcomes
    # false-pass guard: the declared specs all ran (a filtered or empty run fails here)
    assert len(ran) == SPECS_RUN, outcomes
    # every session the browser drove satisfies the server-side invariants
    rows = await pg.run(
        "super",
        "SELECT count(*) FROM assess_sessions WHERE state <> 'STARTING' AND NOT EXISTS ("
        "SELECT 1 FROM assess_turns t WHERE t.session_id = assess_sessions.id "
        "AND t.role = 'bot' AND t.kind = 'opening')",
    )
    assert rows[0][0] == 0
