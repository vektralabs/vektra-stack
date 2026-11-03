"""Server behaviour that needs no fork: framing refusals, size gate, queue and BUSY, socket dir.

The supervisor is replaced by a stub here; real children are the corpus test's job.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from vektra_assess.parser_protocol import (
    Extracted,
    FileStatus,
    encode_frame,
    read_frame,
)
from vektra_assess.parser_sidecar.server import (
    ChildOutcome,
    ParserServer,
    ServerConfig,
    _parse_child_output,
    _peak,
    check_socket_dir,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")


class StubServer(ParserServer):
    def __init__(self, cfg: ServerConfig) -> None:
        super().__init__(cfg)
        self.release = threading.Event()
        self.calls = 0

    def _supervise(self, op, data, ext, max_pages, max_chars, timeout_s):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.release.wait(5)
        return ChildOutcome(Extracted(FileStatus.READ, data.decode()), 1, 0, 0, False)


@pytest.fixture(scope="module")
async def srv(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[StubServer]:
    d = tmp_path_factory.mktemp("srv") / "run"
    d.mkdir(mode=0o700)
    s = StubServer(
        ServerConfig(socket_path=d / "p.sock", pool=1, queue_max=2, max_file_bytes=1024)
    )
    await s.start()
    yield s
    await s.close()


async def _call(
    srv: StubServer, header: dict, payload: bytes = b""
) -> tuple[dict, bytes]:  # type: ignore[type-arg]
    r, w = await asyncio.open_unix_connection(str(srv.cfg.socket_path))
    w.write(encode_frame(header, payload))
    await w.drain()
    out = await read_frame(r, max_payload=10_000)
    w.close()
    return out


def _req(op: str = "parse", **kw: object) -> dict:  # type: ignore[type-arg]
    return {"v": 1, "op": op, "req_id": "r1", "ext_hint": "txt", "limits": {}, **kw}


async def test_socket_is_0600(srv: StubServer) -> None:
    assert stat.S_IMODE(srv.cfg.socket_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "bad",
    [
        {"v": 2, "op": "parse", "req_id": "r"},
        {"v": 1, "op": "format_disk", "req_id": "r"},
        {"v": 1, "op": "parse"},
        {"v": 1, "op": "parse", "req_id": "x" * 65},
        {"v": 1, "op": "parse", "req_id": 5},
    ],
)
async def test_bad_requests_get_bad_request(srv: StubServer, bad: dict) -> None:  # type: ignore[type-arg]
    header, _ = await _call(srv, bad)
    assert header["code"] == "BAD_REQUEST" and header["status"] is None
    assert srv.calls == 0


async def test_garbage_and_oversize_header_do_not_kill_the_server(
    srv: StubServer,
) -> None:
    for raw in (
        b"\x00\x00\x00\x00",
        b"\xff\xff\xff\xff",
        b"\x00\x00\x00\x05abcde",
        b"",
    ):
        r, w = await asyncio.open_unix_connection(str(srv.cfg.socket_path))
        w.write(raw)
        await w.drain()
        w.write_eof()
        await asyncio.wait_for(r.read(), 5)
        w.close()
    header, _ = await _call(srv, {"v": 1, "op": "ping", "req_id": "p"})
    assert header["code"] == "pong"


async def test_too_large_answers_without_a_child(srv: StubServer) -> None:
    header, payload = await _call(srv, _req(), b"x" * 2000)
    assert header["status"] == "TOO_LARGE" and payload == b""
    assert srv.calls == 0


async def test_queue_is_bounded_and_beyond_it_busy(srv: StubServer) -> None:
    srv.release.clear()
    first = asyncio.create_task(_call(srv, {**_req(), "req_id": "a"}, b"1"))
    await asyncio.sleep(0.2)  # a holds the only slot
    queued = [
        asyncio.create_task(_call(srv, {**_req(), "req_id": f"q{i}"}, b"2"))
        for i in range(2)
    ]
    await asyncio.sleep(0.2)
    header, _ = await _call(srv, {**_req(), "req_id": "over"}, b"3")
    assert header["code"] == "BUSY" and header["status"] is None
    srv.release.set()
    done = await asyncio.gather(first, *queued)
    assert [h["status"] for h, _ in done] == ["READ"] * 3


async def test_shallow_ping_needs_no_child(srv: StubServer) -> None:
    before = srv.calls
    header, _ = await _call(srv, {"v": 1, "op": "ping", "req_id": "p2"})
    assert header["code"] == "pong" and header["status"] is None and srv.calls == before


# --- startup refusals and child output schema ----------------------------------------


async def test_socket_dir_must_be_private_and_ours(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    check_socket_dir(d / "s")  # fine
    os.chmod(d, 0o750)
    with pytest.raises(RuntimeError, match="0700"):
        check_socket_dir(d / "s")
    os.chmod(d, 0o707)
    with pytest.raises(RuntimeError, match="0700"):
        check_socket_dir(d / "s")


async def test_child_output_is_schema_checked() -> None:
    ok = b'{"status":"READ","upto_page":null,"pages_total":3,"code":"","chars":2}\nhi'
    assert _parse_child_output(ok, 100) == Extracted(FileStatus.READ, "hi", None, 3, "")
    bad = [
        b"no newline",
        b'{"status":"BOGUS","chars":0}\n',
        b'{"status":"OVER_MAX_FILES","chars":0}\n',
        b'{"status":"READ","chars":99}\nhi',  # chars does not match
        b'{"status":"READ","chars":2}\n\xff\xfe',
        b'{"status":"READ","chars":2,"upto_page":-1}\nhi',
        b'{"status":"READ","chars":2,"code":5}\nhi',
        b"[1]\n",
    ]
    for raw in bad:
        assert _parse_child_output(raw, 100) is None, raw
    assert _parse_child_output(ok, 1) is None  # over the char cap


async def test_hostile_child_output_never_escapes_the_parser() -> None:
    # not a dict, null, a number, absurdly nested JSON, wrong types: all CORRUPT (None)
    hostile = [
        b"[1]\n",
        b"null\n",
        b"7\n",
        b'"x"\n',
        b"[" * 100_000 + b"\n",
        b'{"a":' * 50_000 + b"1" + b"}" * 50_000 + b"\n",
        b'{"status":["READ"],"chars":0}\n',
        b'{"status":"READ","chars":0,"upto_page":"3"}\n',
        b"\xff\xfe\n",
    ]
    for raw in hostile:
        assert _parse_child_output(raw, 100) is None, raw[:30]
        assert _peak(raw, "vm_peak_kb") is None, raw[:30]
    assert _peak(b'{"vm_peak_kb":12}\n', "vm_peak_kb") == 12
    assert _peak(b'{"vm_peak_kb":-1}\n', "vm_peak_kb") is None


@pytest.mark.parametrize("flavour", ["list", "deep", "garbage", "nostatus"])
async def test_a_hostile_child_gets_its_file_corrupt_and_a_reply(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    flavour: str,
) -> None:
    """End to end with a real forked child that sends hostile bytes back: the file ends
    as CORRUPT and the client gets a reply (before the fix the handler died silently and
    the worker saw ParserUnavailableError, which rolls the whole pre-flight back)."""
    import multiprocessing

    import parser_socket_guard

    from vektra_assess.parser_sidecar import server as server_mod
    from vektra_assess.worker.parser_client import ParserClient

    monkeypatch.setattr(
        server_mod, "child_main", parser_socket_guard.hostile_child_main
    )
    d = tmp_path_factory.mktemp("h") / "run"
    d.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("forkserver")
    srv = ParserServer(ServerConfig(socket_path=d / "p.sock", pool=1), ctx)  # type: ignore[arg-type]
    await srv.start()
    try:
        res = await ParserClient(d / "p.sock").parse_file(b"x", flavour)
        assert res.status is FileStatus.CORRUPT and res.code == "child_died"
        assert (await ParserClient(d / "p.sock").ping()) >= 0  # and it keeps serving
    finally:
        await srv.close()


async def test_max_pages_setting_is_an_upper_bound(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    d = tmp_path_factory.mktemp("mp") / "run"
    d.mkdir(mode=0o700)
    s = StubServer(ServerConfig(socket_path=d / "p.sock", max_pages=10, pool=1))
    seen: list[int] = []
    orig = s._supervise

    def spy(op, data, ext, max_pages, max_chars, timeout_s):  # type: ignore[no-untyped-def]
        seen.append(max_pages)
        return orig(op, data, ext, max_pages, max_chars, timeout_s)

    s._supervise = spy  # type: ignore[method-assign]
    s.release.set()
    await s.start()
    try:
        for asked, want in ((50, 10), (5, 5), (999, 10), (0, 1)):
            await _call(s, {**_req(), "limits": {"max_pages": asked}}, b"x")
            assert seen[-1] == want, (asked, seen)
    finally:
        await s.close()


# --- admission: decided from the header, bounded memory, bounded connections ------------


async def _raw(
    path: Path, header: dict, payload: bytes = b""
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:  # type: ignore[type-arg]
    r, w = await asyncio.open_unix_connection(str(path))
    w.write(encode_frame(header, payload))
    await w.drain()
    return r, w


async def _reply_of(r: asyncio.StreamReader, timeout: float = 2.0) -> dict:  # type: ignore[type-arg]
    header, _ = await asyncio.wait_for(read_frame(r, max_payload=10_000), timeout)
    return header


@pytest.fixture
async def small(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[StubServer]:
    d = tmp_path_factory.mktemp("adm") / "run"
    d.mkdir(mode=0o700)
    s = StubServer(
        ServerConfig(
            socket_path=d / "p.sock",
            pool=1,
            queue_max=2,
            max_file_bytes=1000,
            read_deadline_s=0.6,
        )
    )
    await s.start()
    yield s
    s.release.set()
    await s.close()


class _Truncated(bytes):
    """A frame whose header promises a payload that never arrives."""


async def _stalled_sender(
    srv: StubServer, i: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    r, w = await asyncio.open_unix_connection(str(srv.cfg.socket_path))
    frame = encode_frame({**_req(), "req_id": f"s{i}"}, b"x" * 1000)
    w.write(frame[:-900])  # header plus 100 of the 1000 payload bytes, then silence
    await w.drain()
    return r, w


async def test_stalled_senders_cannot_exceed_the_admission_and_byte_budget(
    small: StubServer,
) -> None:
    senders = [await _stalled_sender(small, i) for i in range(8)]
    await asyncio.sleep(0.2)
    admitted_limit = small.cfg.pool + small.cfg.queue_max  # 3
    assert small._admitted == admitted_limit
    assert small._reserved == admitted_limit * 1000 <= small.cfg.payload_budget
    busy = 0
    for r, _w in senders[admitted_limit:]:
        h = await _reply_of(r)
        assert h["code"] == "BUSY"
        busy += 1
    assert busy == 8 - admitted_limit
    # the overall read deadline releases the stalled ones: nothing stays reserved
    for r, _w in senders[:admitted_limit]:
        h = await _reply_of(r, timeout=3)
        assert h["code"] == "BAD_REQUEST"
    await asyncio.sleep(0.1)
    assert (small._admitted, small._reserved, small._open) == (0, 0, 0)
    for _r, w in senders:
        w.close()


async def test_open_connections_are_capped_before_any_byte_is_read(
    small: StubServer,
) -> None:
    conns = []
    for _ in range(small.cfg.max_open):  # connect and say nothing
        conns.append(await asyncio.open_unix_connection(str(small.cfg.socket_path)))
    await asyncio.sleep(0.2)
    assert small._open == small.cfg.max_open
    r, w = await asyncio.open_unix_connection(str(small.cfg.socket_path))
    h = await _reply_of(r)
    assert h["code"] == "BUSY" and h["req_id"] is None
    w.close()
    for _r, cw in conns:
        cw.close()


async def test_a_dripping_sender_hits_the_overall_deadline(small: StubServer) -> None:
    r, w = await asyncio.open_unix_connection(str(small.cfg.socket_path))
    t0 = asyncio.get_running_loop().time()
    reply = asyncio.create_task(_reply_of(r, timeout=3))
    for b in encode_frame({**_req(), "req_id": "drip"}, b"x" * 1000)[:12]:
        if reply.done():
            break  # the server gave up on us: that is the point
        w.write(bytes([b]))
        await asyncio.sleep(0.15)  # every read is "fast"; the whole is not
    h = await reply
    assert h["code"] == "BAD_REQUEST"
    assert (
        asyncio.get_running_loop().time() - t0 < 1.5
    )  # 0.6 s deadline, not 10 s per read
    w.close()


async def test_a_payload_over_the_byte_budget_is_busy_not_buffered(
    small: StubServer,
) -> None:
    # three admitted 1000-byte holders would be 3000 <= 4000; a fourth cannot be admitted
    # either way (admission), so shrink the budget to prove the byte check on its own
    small._reserved = small.cfg.payload_budget - 500  # as if other payloads were held
    h, _ = await _call(small, {**_req(), "req_id": "big"}, b"x" * 800)
    assert h["code"] == "BUSY" and h["req_id"] == "big"
    small._reserved = 0
    small.release.set()


async def test_config_defaults_and_derived_bounds() -> None:
    cfg = ServerConfig(socket_path=Path("/x/s"))
    assert (cfg.pool, cfg.timeout_s, cfg.max_chars, cfg.queue_max) == (
        3,
        10.0,
        200_000,
        32,
    )
    assert cfg.max_open == 3 + 32 + 8
    assert cfg.payload_budget == 4 * cfg.max_file_bytes == 40 * 1024 * 1024


# --- independent review: fds on a failed fork, locked counters, no dead constants -----------


async def test_a_failed_start_closes_all_six_pipe_ends() -> None:
    import multiprocessing

    from vektra_assess.parser_sidecar import server as server_mod

    ctx = multiprocessing.get_context("forkserver")

    class Boom:
        def __init__(self, *a, **k) -> None:  # type: ignore[no-untyped-def]
            pass

        def start(self) -> None:
            raise OSError("fork failed")

    class Ctx:
        Pipe = staticmethod(ctx.Pipe)
        Process = Boom

    srv = ParserServer(ServerConfig(socket_path=Path("/x/s")), Ctx())  # type: ignore[arg-type]

    def fds() -> int:
        return len(os.listdir("/dev/fd"))

    before = fds()
    for _ in range(40):
        with pytest.raises(OSError, match="fork failed"):
            srv._supervise("parse", b"x", "txt", 50, 1000, 1.0)
    assert fds() - before <= 1, (before, fds())  # 40 failures x 6 ends would be +240
    assert srv.children_spawned == 0
    assert not hasattr(server_mod, "IO_TIMEOUT_S")


async def test_spawn_bookkeeping_is_exact_under_thread_contention() -> None:
    import sys

    srv = ParserServer(ServerConfig(socket_path=Path("/x/s")))
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:

        def hammer() -> None:
            for i in range(2000):
                srv._note_spawn(i)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert srv.children_spawned == 8 * 2000
    assert len(srv.child_pids) == 1000
