"""Chaos: the parser sidecar killed in the middle of an extraction (plan 13a, ARCH-066, REQ-071).

The real sidecar (``vektra_assess.parser_sidecar.server``: supervisor, forkserver, one child per
item) runs as its own process group on a private unix socket; the worker side is the production
``ParserClient`` inside the production pre-flight, over the recorded Moodle 4.3 site.

The kill point: the first extraction request of the capture is on the sidecar's socket and no
reply has been sent. The sidecar is frozen (SIGSTOP) before the request so the request cannot be
answered first, then the whole group gets SIGKILL: supervisor, forkserver and item child, no
handler, no reply, the socket closed by the kernel. The client sees a killed peer before any
reply byte: ``closed_without_reply`` (EOF, macOS) or ``reset`` (ECONNRESET, Linux), the two
``_Unavailable`` outcomes of ``ParserClient._read_response``. Both are the parser unavailable,
never a "corrupt file" in the snapshot, and both roll back with the same reason.

Rows: the pre-flight rolls back, the STARTING row is deleted, snapshot / key / turn / hand-off rows
are 0, one ``rolled_back`` attempt and one ``assess_start_rollback`` audit row. The sidecar is
started again (what ``restart: unless-stopped`` does), the student starts again, and the same
path, unkilled, commits exactly one ACTIVE session: that run is the negative control, proving the
rollback above is caused by the kill and that the capture really goes through the parser.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest
from assess_moodle_support import Replay, make_client
from sqlalchemy.ext.asyncio import create_async_engine
from support.preflight import FakePartB, activate, deps, job_context, kek_ring
from support.sessions import _site_release, fixture_target
from support.world import configure_activity, world

from integration.test_session_preflight import _reset
from vektra_assess.worker.parser_client import ParserClient, ParserUnavailableError
from vektra_assess.worker.preflight import make_preflight_handler
from vektra_assess.worker.start_source import MoodleStartSource

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "tests" / "chaos" / "assess")
)
from invariants import assert_invariants

pytestmark = pytest.mark.integration

#: what the client reports for a peer killed before any reply byte: EOF on macOS, ECONNRESET
#: on Linux (both ``_Unavailable`` -> ``ParserUnavailableError``). ``reset_mid_reply`` or
#: ``truncated`` would mean a partial reply (a protocol violation, CORRUPT), so they are not here
KILLED_PEER = frozenset({"closed_without_reply", "reset"})

SIDECAR = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from vektra_assess.parser_sidecar.server import ServerConfig, main
    raise SystemExit(main(ServerConfig(socket_path=Path(sys.argv[1]), pool=1)))
    """
)


class Sidecar:
    """The parser sidecar as a process group (so its forkserver and children die with it)."""

    def __init__(self, socket_path: Path, log: Path) -> None:
        self.socket_path = socket_path
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None

    async def start(self) -> Sidecar:
        with self.log.open("ab") as fh:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", SIDECAR, str(self.socket_path)],
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        client = ParserClient(self.socket_path, connect_timeout_s=1.0)
        for _ in range(300):
            assert self.proc.poll() is None, self.log.read_text(errors="replace")
            try:
                await client.ping()
                return self
            except ParserUnavailableError:
                await asyncio.sleep(0.1)
        raise AssertionError("the parser sidecar never answered a ping")

    def freeze(self) -> None:
        assert self.proc is not None
        os.killpg(self.proc.pid, signal.SIGSTOP)

    def kill9(self) -> None:
        assert self.proc is not None
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(10)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(10)


class KillPoint:
    """The production ``ParserClient``, with the chaos hook on its first extraction call."""

    def __init__(self, client: ParserClient, sidecar: Sidecar, *, kill: bool) -> None:
        self.client = client
        self.sidecar = sidecar
        self.kill = kill
        self.calls: list[str] = []
        self.errors: list[str] = []

    async def _call(self, op: str, coro_fn: Any, *args: Any) -> Any:
        self.calls.append(op)
        if not (self.kill and len(self.calls) == 1):
            return await coro_fn(*args)
        self.sidecar.freeze()  # the request lands on a socket nobody can answer yet
        task = asyncio.ensure_future(coro_fn(*args))
        await asyncio.sleep(0.5)
        assert not task.done(), "the frozen sidecar answered"
        self.sidecar.kill9()
        try:
            return await task
        except ParserUnavailableError as exc:
            self.errors.append(str(exc))
            raise

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Any:
        return await self._call("parse", self.client.parse_file, data, ext_hint)

    async def sanitize_html(self, html: bytes) -> Any:
        return await self._call("sanitize_html", self.client.sanitize_html, html)

    async def extract_review_item(self, html: bytes) -> Any:
        return await self._call("review_item", self.client.extract_review_item, html)


async def rows(pg: Any, site: Any, cmid: int, sub: int) -> dict[str, int]:
    got = (
        await pg.run(
            "super",
            "SELECT (SELECT count(*) FROM assess_sessions WHERE site_id = :s AND cmid = :c "
            " AND student_userid = :u), "
            "(SELECT count(*) FROM assess_sessions WHERE site_id = :s AND cmid = :c "
            " AND student_userid = :u AND state = 'ACTIVE'), "
            "(SELECT count(*) FROM assess_snapshots p JOIN assess_sessions s ON s.id = p.session_id "
            " WHERE s.site_id = :s), "
            "(SELECT count(*) FROM assess_session_keys k JOIN assess_sessions s "
            " ON s.id = k.session_id WHERE s.site_id = :s), "
            "(SELECT count(*) FROM assess_turns t JOIN assess_sessions s ON s.id = t.session_id "
            " WHERE s.site_id = :s), "
            "(SELECT count(*) FROM assess_session_handoff h JOIN assess_sessions s "
            " ON s.id = h.session_id WHERE s.site_id = :s), "
            "(SELECT count(*) FROM assess_start_attempts WHERE site_id = :s AND cmid = :c "
            " AND student_userid = :u AND outcome = 'rolled_back'), "
            "(SELECT count(*) FROM assess_audit_events WHERE site_id = :s "
            " AND event = 'assess_start_rollback')",
            {"s": site, "c": cmid, "u": sub},
        )
    )[0]
    keys = (
        "sessions",
        "active",
        "snapshots",
        "keys",
        "turns",
        "handoff",
        "rolled_back",
        "rb_audit",
    )
    return dict(zip(keys, (int(x) for x in got), strict=True))


async def _start(w: Any, sub: int, cmid: int) -> uuid.UUID:
    token = await w.presession(sub=sub, cmid=cmid)
    r = await w.client.post(
        "/api/v1/assess/sessions", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 202, r.text
    return uuid.UUID(r.json()["session_id"])


async def test_parser_killed_mid_extraction_rolls_the_start_back_then_restarts_clean(
    pg,
) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    # AF_UNIX paths are short (104 bytes on macOS): a private 0700 directory under /tmp
    sock_dir = Path(tempfile.mkdtemp(prefix="vxp-", dir="/tmp"))
    sidecar = Sidecar(sock_dir / "p.sock", sock_dir / "sidecar.log")
    worker = create_async_engine(pg.url("worker"))
    try:
        await sidecar.start()
        async with world(pg) as w:
            sub, cmid = fixture_target()
            await _site_release(w, "4.3")
            await configure_activity(pg, w.site, cmid)
            await activate(pg)
            kek = kek_ring()
            empty = await rows(pg, w.site, cmid, sub)
            assert set(empty.values()) == {0}

            def handler(parser: Any) -> Any:
                source = MoodleStartSource(
                    make_client(Replay("4.3")), parser, worker.connect
                )
                return make_preflight_handler(
                    deps(w.settings, w.handoff_public, source, FakePartB(), kek=kek)
                )

            # 1. the kill, mid-extraction
            killed = KillPoint(ParserClient(sidecar.socket_path), sidecar, kill=True)
            sid = await _start(w, sub, cmid)
            await handler(killed)(job_context(worker, sid))
            assert killed.calls[:1] and len(killed.errors) == 1, (
                killed.calls,
                killed.errors,
            )
            assert killed.errors[0] in KILLED_PEER, killed.errors
            assert sidecar.proc is not None and sidecar.proc.poll() is not None
            after_kill = await rows(pg, w.site, cmid, sub)
            assert after_kill == {**empty, "rolled_back": 1, "rb_audit": 1}, after_kill
            reason = await pg.run(
                "super",
                "SELECT reason_code FROM assess_start_attempts WHERE session_id = :s "
                "AND outcome = 'rolled_back'",
                {"s": sid},
            )
            assert [r[0] for r in reason] == ["internal_error"]
            await assert_invariants(pg, "parser-kill:mid-extraction", expect_sessions=0)

            # 2. the sidecar restarted; the same path, unkilled, commits once (negative control)
            await sidecar.start()
            clean = KillPoint(ParserClient(sidecar.socket_path), sidecar, kill=False)
            sid2 = await _start(w, sub, cmid)
            await handler(clean)(job_context(worker, sid2))
            assert clean.calls, "the capture did not go through the parser at all"
            assert clean.calls[0] == killed.calls[0]  # the very call that was killed
            committed = await rows(pg, w.site, cmid, sub)
            assert committed["sessions"] == committed["active"] == 1, committed
            assert (
                committed["snapshots"],
                committed["keys"],
                committed["handoff"],
            ) == (1, 1, 1)
            assert committed["turns"] >= 1
            assert (committed["rolled_back"], committed["rb_audit"]) == (1, 1)
            await assert_invariants(
                pg, "parser-kill:restarted", expect_sessions=1, sites=[w.site]
            )
    finally:
        sidecar.stop()
        await worker.dispose()
        shutil.rmtree(sock_dir, ignore_errors=True)
