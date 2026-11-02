"""The parser sidecar server: asyncio unix socket, supervisor, pool (COMP-008, ARCH-066).

The parent never parses. Each item runs in a new child process (forkserver, one item per
process, never reused), under rlimits, SIGKILLed at the wall-clock timeout. Whatever the
child does, the parent answers exactly one status and survives. Logs carry codes only:
``req_id``, op, status, duration, child exit code, stderr byte count; never text and never
a filename (the filename is not even on the wire).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import signal
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.context import ForkServerContext
from pathlib import Path
from typing import Any

from vektra_assess.parser_protocol import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIMEOUT_MS,
    OPS,
    PROTOCOL_VERSION,
    TRANSPORT_BUSY,
    Extracted,
    FileStatus,
    FrameError,
    encode_frame,
    parse_header,
    parse_header_length,
)
from vektra_assess.parser_sidecar.child import PRELOAD, child_main
from vektra_assess.parser_sidecar.ping import ping_pdf

log = logging.getLogger("vektra_assess.parser")

IO_TIMEOUT_S = 10.0  # a peer that sends a header and stalls must not hold a task
DISCARD_LIMIT = 64 * 1024 * 1024
MiB = 1024 * 1024


@dataclass(frozen=True)
class ServerConfig:
    socket_path: Path
    pool: int = 3
    timeout_s: float = DEFAULT_TIMEOUT_MS / 1000
    max_chars: int = DEFAULT_MAX_CHARS
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_pages: int = DEFAULT_MAX_PAGES
    rlimit_as: int = 256 * MiB
    queue_max: int = 32

    read_deadline_s: float = (
        30.0  # whole-connection budget to receive header and payload
    )

    @property
    def max_open(self) -> int:
        """Open connections allowed: admitted requests plus a few still being read."""
        return self.pool + self.queue_max + 8

    @property
    def payload_budget(self) -> int:
        """Bytes of payload held at once (running + queued): bounds the supervisor's RSS."""
        return 4 * self.max_file_bytes


def check_socket_dir(path: Path) -> None:
    """Refuse to start unless the socket directory is private (0700) and ours."""
    st = path.parent.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(
            f"socket directory {path.parent} must be a directory owned by uid "
            f"{os.getuid()} with mode 0700 (found uid {st.st_uid}, mode "
            f"{stat.S_IMODE(st.st_mode):o})"
        )


def _event(event: str, **fields: Any) -> None:
    log.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


@dataclass
class ChildOutcome:
    result: Extracted
    pid: int | None
    exit_code: int | None
    stderr_bytes: int
    timed_out: bool
    vm_peak_kb: int | None = None
    rss_peak_kb: int | None = None


def _load_head(raw: bytes) -> dict[str, Any] | None:
    """The first line of the child's output as a dict, or None (the child is hostile too)."""
    try:
        head = json.loads(raw.partition(b"\n")[0])
    except (ValueError, RecursionError):
        return None
    return head if isinstance(head, dict) else None


def _peak(raw: bytes, key: str) -> int | None:
    head = _load_head(raw)
    v = head.get(key) if head else None
    return v if type(v) is int and v >= 0 else None


def _parse_child_output(raw: bytes, max_chars: int) -> Extracted | None:
    """The child is not trusted either: schema-check what it sent. None means CORRUPT."""
    head = _load_head(raw)
    _, sep, body = raw.partition(b"\n")
    if head is None or not sep:
        return None
    try:
        status = FileStatus(head["status"])
        text = body.decode("utf-8")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    up, pt, code = head.get("upto_page"), head.get("pages_total"), head.get("code", "")
    if status is FileStatus.OVER_MAX_FILES or not isinstance(code, str):
        return None
    if len(text) > max_chars or head.get("chars") != len(text):
        return None
    for v in (up, pt):
        if v is not None and (type(v) is not int or v < 0):
            return None
    return Extracted(status, text, up, pt, code[:64])


class ParserServer:
    def __init__(self, cfg: ServerConfig, ctx: ForkServerContext | None = None) -> None:
        self.cfg = cfg
        self.ctx = ctx or multiprocessing.get_context("forkserver")
        self._executor = ThreadPoolExecutor(max_workers=cfg.pool + 1)
        self._sem = asyncio.Semaphore(cfg.pool)
        self._admitted = 0  # requests past admission: running, queued or being read
        self._open = 0
        self._reserved = 0  # payload bytes reserved by admitted requests
        self._deep_lock = asyncio.Lock()
        self.children_spawned = (
            0  # the counter the "0 children for TOO_LARGE" test reads
        )
        self.child_pids: list[int] = []
        self.server: asyncio.base_events.Server | None = None

    # --- lifecycle ---------------------------------------------------------------

    async def start(self) -> asyncio.base_events.Server:
        path = self.cfg.socket_path
        check_socket_dir(path)
        if path.exists() or path.is_symlink():
            path.unlink()  # stale path from a previous run
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._handle, path=str(path))
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        return self.server

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self._executor.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(FileNotFoundError):
            self.cfg.socket_path.unlink()

    # --- one connection -----------------------------------------------------------

    async def _reply(
        self, writer: asyncio.StreamWriter, header: dict[str, Any], payload: bytes = b""
    ) -> None:
        writer.write(encode_frame({"v": PROTOCOL_VERSION, **header}, payload))
        await writer.drain()

    async def _busy(
        self, writer: asyncio.StreamWriter, req_id: str | None = None
    ) -> None:
        await self._reply(
            writer, {"req_id": req_id, "status": None, "code": TRANSPORT_BUSY}
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        t0 = time.monotonic()
        req_id: Any = None
        op = "?"
        status = "-"
        reserved = 0
        admitted = False
        counted = False
        try:
            if self._open >= self.cfg.max_open:
                # Too many open connections (slow or stalled senders): refuse before
                # reading a byte, so the supervisor's memory stays bounded.
                await self._busy(writer)
                status = TRANSPORT_BUSY
                return
            self._open += 1
            counted = True
            try:
                async with asyncio.timeout(self.cfg.read_deadline_s):
                    # one deadline for the whole receive phase: a sender that drips
                    # bytes, each read inside a per-read timeout, still runs out of it
                    n = parse_header_length(await reader.readexactly(4))
                    header = parse_header(await reader.readexactly(n))
                    req_id = header.get("req_id")
                    op = str(header.get("op"))
                    if (
                        header.get("v") != PROTOCOL_VERSION
                        or op not in OPS
                        or not isinstance(req_id, str)
                        or not 0 < len(req_id) <= 64
                    ):
                        raise FrameError("bad request")
                    plen = header["payload_len"]
                    if op == "ping":
                        status = await self._ping(
                            writer, req_id, bool(header.get("deep"))
                        )
                        return
                    if plen > self.cfg.max_file_bytes:
                        # Over the cap: answered without forking. Drain a bounded
                        # amount so a well-behaved client finishes its send.
                        await self._discard(reader, plen)
                        await self._reply(
                            writer,
                            {
                                "req_id": req_id,
                                **_wire(
                                    Extracted(FileStatus.TOO_LARGE, code="too_large")
                                ),
                            },
                        )
                        status = "TOO_LARGE"
                        return
                    # Admission is decided from the header, before the payload is read
                    if (
                        self._admitted >= self.cfg.pool + self.cfg.queue_max
                        or self._reserved + plen > self.cfg.payload_budget
                    ):
                        await self._busy(writer, req_id)
                        status = TRANSPORT_BUSY
                        return
                    self._admitted += 1
                    admitted = True
                    self._reserved += plen
                    reserved = plen
                    payload = await reader.readexactly(plen)
            except (FrameError, asyncio.IncompleteReadError, TimeoutError):
                if status == "-":
                    status = "BAD_REQUEST"
                    with contextlib.suppress(Exception):
                        await self._reply(
                            writer,
                            {"req_id": None, "status": None, "code": "BAD_REQUEST"},
                        )
                return
            raw_lim = header.get("limits")
            lim: dict[str, Any] = raw_lim if isinstance(raw_lim, dict) else {}
            max_pages = _clamp(
                lim.get("max_pages"), 1, self.cfg.max_pages, self.cfg.max_pages
            )
            max_chars = _clamp(
                lim.get("max_chars"), 1000, self.cfg.max_chars, self.cfg.max_chars
            )
            timeout_s = (
                _clamp(
                    lim.get("timeout_ms"),
                    100,
                    int(self.cfg.timeout_s * 1000),
                    int(self.cfg.timeout_s * 1000),
                )
                / 1000
            )
            ext = _ext(header.get("ext_hint"))
            async with self._sem:
                out = await asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    self._supervise,
                    str(op),
                    payload,
                    ext,
                    max_pages,
                    max_chars,
                    timeout_s,
                )
            status = out.result.status.value
            _event(
                "parse",
                req_id=req_id,
                op=op,
                status=status,
                code=out.result.code,
                duration_ms=int((time.monotonic() - t0) * 1000),
                child_pid=out.pid,
                exit_code=out.exit_code,
                stderr_bytes=out.stderr_bytes,
                timed_out=out.timed_out,
                vm_peak_kb=out.vm_peak_kb,
                rss_peak_kb=out.rss_peak_kb,
            )
            await self._reply(
                writer,
                {"req_id": req_id, **_wire(out.result)},
                out.result.text.encode("utf-8", "replace"),
            )
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:  # the supervisor must survive anything
            _event(
                "handler_error",
                req_id=req_id if isinstance(req_id, str) else None,
                error=type(exc).__name__,
            )
        finally:
            if reserved:
                self._reserved -= reserved
            if admitted:
                self._admitted -= 1
            if counted:
                self._open -= 1
            if status in (TRANSPORT_BUSY, "BAD_REQUEST", "-", "TOO_LARGE", "pong"):
                _event(
                    "request",
                    req_id=req_id if isinstance(req_id, str) else None,
                    op=op if isinstance(op, str) else None,
                    status=status,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _discard(reader: asyncio.StreamReader, n: int) -> None:
        left = min(n, DISCARD_LIMIT)
        while left > 0:
            chunk = await reader.read(min(left, 1 << 20))
            if not chunk:
                return
            left -= len(chunk)

    async def _ping(self, writer: asyncio.StreamWriter, req_id: str, deep: bool) -> str:
        if not deep:
            await self._reply(
                writer, {"req_id": req_id, "status": None, "code": "pong"}
            )
            return "pong"
        async with (
            self._deep_lock
        ):  # its own lane: the check must not queue behind a burst
            out = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                self._supervise,
                "parse",
                ping_pdf(),
                "pdf",
                1,
                1000,
                8.0,
            )
        await self._reply(
            writer,
            {"req_id": req_id, "status": out.result.status.value, "code": "pong"},
        )
        return "pong"

    # --- the supervisor ---------------------------------------------------------------

    def _supervise(
        self,
        op: str,
        data: bytes,
        ext: str,
        max_pages: int,
        max_chars: int,
        timeout_s: float,
    ) -> ChildOutcome:
        """Runs in a worker thread: spawn one child, feed it, kill it at the deadline."""
        spec = {
            "op": op,
            "ext_hint": ext,
            "max_pages": max_pages,
            "max_chars": max_chars,
            "max_file_bytes": self.cfg.max_file_bytes,
            "timeout_ms": int(timeout_s * 1000),
            "rlimit_as": self.cfg.rlimit_as,
        }
        in_r, in_w = self.ctx.Pipe(duplex=False)
        out_r, out_w = self.ctx.Pipe(duplex=False)
        err_r, err_w = self.ctx.Pipe(duplex=False)
        proc = self.ctx.Process(target=child_main, args=(in_r, out_w, err_w, spec))
        proc.start()
        self.children_spawned += 1
        pid = proc.pid
        if pid is not None:
            self.child_pids.append(pid)
            del self.child_pids[:-1000]
        for c in (in_r, out_w, err_w):
            c.close()
        counted = [0]

        def drain() -> None:
            fd = err_r.fileno()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                counted[0] += len(chunk)  # bytes counted and discarded, never logged

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            with contextlib.suppress(Exception):
                proc.kill()

        timer = threading.Timer(timeout_s, kill)
        timer.start()
        result: Extracted | None = None
        raw = b""
        try:
            try:
                in_w.send_bytes(data)
            except (BrokenPipeError, OSError):
                pass
            try:
                raw = out_r.recv_bytes(max_chars * 4 + 4096)
                result = _parse_child_output(raw, max_chars)
            except (EOFError, OSError, ValueError, RecursionError):
                result = None
        finally:
            timer.cancel()
            for c in (in_w, out_r):
                with contextlib.suppress(Exception):
                    c.close()
            proc.join(2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(2.0)
            drainer.join(1.0)
            with contextlib.suppress(Exception):
                err_r.close()
            exit_code = proc.exitcode
            with contextlib.suppress(Exception):
                proc.close()
        if result is None:
            code = "timeout" if timed_out.is_set() else "child_died"
            result = Extracted(FileStatus.CORRUPT, code=code)
        return ChildOutcome(
            result,
            pid,
            exit_code,
            counted[0],
            timed_out.is_set(),
            _peak(raw, "vm_peak_kb"),
            _peak(raw, "rss_peak_kb"),
        )


def _wire(r: Extracted) -> dict[str, Any]:
    return {
        "status": r.status.value,
        "upto_page": r.upto_page,
        "pages_total": r.pages_total,
        "chars": len(r.text),
        "code": r.code,
    }


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    if type(v) is not int:
        return default
    return max(lo, min(hi, v))


def _ext(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    v = v.lower().lstrip(".")[:16]
    return v if v.isalnum() else ""


async def serve(cfg: ServerConfig) -> int:
    ctx = multiprocessing.get_context("forkserver")
    ctx.set_forkserver_preload(list(PRELOAD))
    from multiprocessing import forkserver

    forkserver.ensure_running()  # start (and preload) now, not on the first request
    srv = ParserServer(cfg, ctx)
    await srv.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    _event(
        "started",
        pool=cfg.pool,
        timeout_s=cfg.timeout_s,
        rlimit_as_mb=cfg.rlimit_as // MiB,
    )
    try:
        await stop.wait()
    finally:
        await srv.close()
    return 0


def main(cfg: ServerConfig) -> int:
    """Serve until SIGTERM. The config is built by the composition root (vektra_app)."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    return asyncio.run(serve(cfg))
