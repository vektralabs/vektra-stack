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


async def test_config_from_env(tmp_path: Path) -> None:
    cfg = ServerConfig.from_env(
        tmp_path / "s",
        {
            "VEKTRA_ASSESS_PARSER_POOL": "5",
            "VEKTRA_ASSESS_PARSER_TIMEOUT_S": "7",
            "VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB": "300",
        },
    )
    assert (cfg.pool, cfg.timeout_s, cfg.rlimit_as) == (5, 7.0, 300 * 1024 * 1024)
    d = ServerConfig.from_env(tmp_path / "s", {})
    assert (d.pool, d.timeout_s, d.max_chars, d.queue_max) == (3, 10.0, 200_000, 32)
