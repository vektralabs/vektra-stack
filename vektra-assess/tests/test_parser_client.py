"""The worker-side client against a fake sidecar that lies (plan assess-parser, task D).

Every malformed reply must come out as CORRUPT for that file, never as text; an absent
or unresponsive sidecar must raise ParserUnavailableError, never parse locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import structlog

from vektra_assess.moodle.setup_check import MISSING, OK, ItemRegistry
from vektra_assess.parser_protocol import FileStatus, Limits, encode_frame, read_frame
from vektra_assess.worker.parser_client import (
    ParserClient,
    ParserUnavailableError,
    parser_setup_check,
    register_parser_check,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

Responder = Callable[[dict[str, Any], bytes], Awaitable[bytes | None]]


def reply(req: dict[str, Any], **over: Any) -> bytes:
    text = over.pop("text", "")
    head = {
        "v": 1,
        "req_id": req["req_id"],
        "status": "READ",
        "upto_page": None,
        "pages_total": None,
        "chars": len(text),
        "code": "",
        **over,
    }
    return encode_frame(head, text.encode() if isinstance(text, str) else text)


class Fake:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.responder: Responder = self._default
        self.requests: list[dict[str, Any]] = []
        self.server: asyncio.base_events.Server | None = None

    @staticmethod
    async def _default(req: dict[str, Any], payload: bytes) -> bytes | None:
        return reply(req, text="ok")

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            req, payload = await read_frame(r, max_payload=1 << 24)
            self.requests.append(req)
            raw = await self.responder(req, payload)
            if raw is not None:
                w.write(raw)
                await w.drain()
        except Exception:
            pass
        finally:
            w.close()

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


@pytest.fixture
async def fake() -> AsyncIterator[Fake]:
    d = Path(tempfile.mkdtemp(prefix="vp", dir="/tmp"))  # sun_path is short on macOS
    f = Fake(d / "f.sock")
    await f.start()
    yield f
    await f.stop()
    shutil.rmtree(d, ignore_errors=True)


def client_for(f: Fake, **kw: Any) -> ParserClient:
    kw.setdefault("limits", Limits(timeout_ms=1000, max_chars=1000, max_pages=50))
    kw.setdefault("queue_allowance_s", 1.0)
    kw.setdefault("busy_backoff_s", 0.01)
    return ParserClient(f.path, **kw)


async def test_happy_path_and_request_shape(fake: Fake) -> None:
    res = await client_for(fake).parse_file(b"abc", "txt")
    assert (res.status, res.text, res.chars) == (FileStatus.READ, "ok", 2)
    req = fake.requests[0]
    assert req["op"] == "parse" and req["ext_hint"] == "txt" and req["v"] == 1
    assert req["payload_len"] == 3 and "filename" not in req
    assert req["limits"] == {"max_pages": 50, "max_chars": 1000, "timeout_ms": 1000}


async def test_sanitize_html_op(fake: Fake) -> None:
    await client_for(fake).sanitize_html(b"<p>x</p>")
    assert fake.requests[0]["op"] == "sanitize_html"


async def test_review_item_op_and_reply_parsing(fake: Fake) -> None:
    import json

    fake.responder = lie(
        lambda r: reply(
            r, text=json.dumps({"qtext": "Q", "response": "[x] A", "truncated": True})
        )
    )
    out = await client_for(fake).extract_review_item(b"<div class='qtext'>x</div>")
    assert fake.requests[0]["op"] == "review_item" and out == ("Q", "[x] A", True)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"qtext": "Q"}',
        '["Q", "A"]',
        '{"qtext": 1, "response": null}',
        '{"qtext": "Q", "response": "A"}',  # a reply without the truncation flag is not trusted
        # a cut flag that is not a bool is malformed, not "not truncated"
        '{"qtext": "Q", "response": "A", "truncated": "yes"}',
        '{"qtext": "Q", "response": "A", "truncated": 1}',
        '{"qtext": "Q", "response": "A", "truncated": null}',
    ],
)
async def test_a_bad_review_item_reply_degrades_to_nothing(
    fake: Fake, text: str
) -> None:
    fake.responder = lie(lambda r: reply(r, text=text))
    assert await client_for(fake).extract_review_item(b"<p>x</p>") == (
        None,
        None,
        False,
    )


def lie(make: Callable[[dict[str, Any]], bytes | None]) -> Responder:
    async def responder(req: dict[str, Any], payload: bytes) -> bytes | None:
        return make(req)

    return responder


def _frame_with_header(raw_header: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", len(raw_header)) + raw_header + payload


def _hdr(req: dict[str, Any], **over: Any) -> bytes:
    h = {
        "v": 1,
        "req_id": req["req_id"],
        "status": "READ",
        "upto_page": None,
        "pages_total": None,
        "chars": 2,
        "code": "",
        "payload_len": 2,
        **over,
    }
    return json.dumps(h).encode()


MALFORMED: dict[str, Callable[[dict[str, Any]], bytes]] = {
    "oversize_payload": lambda r: reply(
        r, text="x" * 5000, chars=5000
    ),  # > max_chars * 4
    "bad_utf8": lambda r: _frame_with_header(_hdr(r), b"\xff\xfe"),
    "unknown_status": lambda r: reply(r, status="PWNED", text="hi"),
    "over_max_files": lambda r: reply(r, status="OVER_MAX_FILES"),
    "status_missing": lambda r: reply(r, status=None),
    "req_id_mismatch": lambda r: reply({"req_id": "someone-else"}, text="hi"),
    "wrong_version": lambda r: reply(r, v=2, text="hi"),
    "truncated_payload": lambda r: _frame_with_header(_hdr(r, payload_len=50), b"hi"),
    "truncated_header": lambda r: struct.pack(">I", 200) + b'{"v":1',
    "header_oversize": lambda r: struct.pack(">I", 1 << 20) + b"x" * 64,
    "header_not_json": lambda r: _frame_with_header(b"this is not json"),
    "header_is_list": lambda r: _frame_with_header(b"[1,2]"),
    "negative_payload_len": lambda r: _frame_with_header(_hdr(r, payload_len=-1)),
    "chars_mismatch": lambda r: reply(r, text="hello", chars=2),
    "upto_page_out_of_range": lambda r: reply(
        r, status="READ_PARTIAL", upto_page=999, text="hi"
    ),
    "upto_page_on_wrong_status": lambda r: reply(
        r, status="READ", upto_page=3, text="hi"
    ),
    "text_with_corrupt_status": lambda r: reply(
        r, status="CORRUPT", text="exfiltrated"
    ),
    "code_not_string": lambda r: reply(r, code=["x"], text="hi"),
    "code_too_long": lambda r: reply(r, code="x" * 200, text="hi"),
    "page_field_not_int": lambda r: reply(r, pages_total="3", text="hi"),
    "chars_not_int": lambda r: reply(r, chars="2", text="hi"),
    "reset_mid_reply": lambda r: struct.pack(">I", 100) + b"{",
}


@pytest.mark.parametrize("name", list(MALFORMED))
async def test_malformed_reply_is_corrupt_and_never_text(fake: Fake, name: str) -> None:
    fake.responder = lie(MALFORMED[name])
    with structlog.testing.capture_logs() as logs:
        res = await client_for(fake).parse_file(b"abc", "txt")
    assert res.status is FileStatus.CORRUPT, name
    assert res.text == "" and res.code == "protocol_violation"
    events = [e for e in logs if e["event"] == "assess_parser_protocol_violation"]
    assert len(events) == 1
    assert set(events[0]) <= {"event", "log_level", "reason_code"}  # content-free
    assert "exfiltrated" not in json.dumps(events)


async def test_control_characters_are_stripped_and_nfc_not_required(fake: Fake) -> None:
    text = "a\x00b\x07c\x1bd\te\nf\x7f"
    fake.responder = lie(lambda r: reply(r, text=text))
    res = await client_for(fake).parse_file(b"abc", "txt")
    assert res.text == "abcd\te\nf" and res.chars == len(res.text)


async def test_partial_label_and_valid_page_fields(fake: Fake) -> None:
    fake.responder = lie(
        lambda r: reply(
            r, status="READ_PARTIAL", upto_page=50, pages_total=51, text="hi"
        )
    )
    res = await client_for(fake).parse_file(b"x", "pdf")
    assert res.label == "letto parzialmente (fino a pagina 50)"


async def test_busy_is_retried_then_succeeds(fake: Fake) -> None:
    n = {"i": 0}

    async def responder(req: dict[str, Any], payload: bytes) -> bytes | None:
        n["i"] += 1
        if n["i"] < 3:
            return encode_frame(
                {"v": 1, "req_id": req["req_id"], "status": None, "code": "BUSY"}
            )
        return reply(req, text="ok")

    fake.responder = responder
    res = await client_for(fake).parse_file(b"x", "txt")
    assert res.status is FileStatus.READ and n["i"] == 3


async def test_busy_forever_is_unavailable(fake: Fake) -> None:
    fake.responder = lie(
        lambda r: encode_frame(
            {"v": 1, "req_id": r["req_id"], "status": None, "code": "BUSY"}
        )
    )
    with pytest.raises(ParserUnavailableError, match="busy"):
        await client_for(fake, busy_retries=2).parse_file(b"x", "txt")
    assert len(fake.requests) == 3  # bounded


async def test_socket_missing_refused_silent_and_stalled(
    tmp_path: Path, fake: Fake
) -> None:
    with pytest.raises(ParserUnavailableError):
        await ParserClient(fake.path.parent / "nope.sock").parse_file(b"x", "txt")
    stale = fake.path.parent / "stale.sock"  # a socket file nobody listens on
    import socket

    s = socket.socket(socket.AF_UNIX)
    s.bind(str(stale))
    s.close()
    with pytest.raises(ParserUnavailableError):
        await ParserClient(stale).parse_file(b"x", "txt")
    fake.responder = lie(lambda r: None)  # closes without a byte
    with pytest.raises(ParserUnavailableError, match="closed_without_reply"):
        await client_for(fake).parse_file(b"x", "txt")

    async def stall(req: dict[str, Any], payload: bytes) -> bytes | None:
        await asyncio.sleep(1.5)
        return None

    fake.responder = stall
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(ParserUnavailableError, match="client_timeout"):
        await client_for(
            fake, limits=Limits(timeout_ms=200), queue_allowance_s=0.3
        ).parse_file(b"x", "txt")
    assert asyncio.get_running_loop().time() - t0 < 2  # within the client budget


async def test_ping_shallow_and_deep(fake: Fake) -> None:
    fake.responder = lie(
        lambda r: encode_frame(
            {"v": 1, "req_id": r["req_id"], "status": "READ", "code": "pong"}
        )
    )
    c = client_for(fake)
    assert await c.ping() >= 0
    assert await c.ping(deep=True) >= 0
    assert fake.requests[-1]["deep"] is True
    fake.responder = lie(
        lambda r: encode_frame(
            {"v": 1, "req_id": r["req_id"], "status": "CORRUPT", "code": "pong"}
        )
    )
    with pytest.raises(ParserUnavailableError, match="deep_ping_failed"):
        await c.ping(deep=True)
    fake.responder = lie(
        lambda r: encode_frame(
            {"v": 1, "req_id": "other", "status": None, "code": "pong"}
        )
    )
    with pytest.raises(ParserUnavailableError):
        await c.ping()


async def test_setup_check_contributor(fake: Fake) -> None:
    fake.responder = lie(
        lambda r: encode_frame(
            {"v": 1, "req_id": r["req_id"], "status": "READ", "code": "pong"}
        )
    )
    check = parser_setup_check(client_for(fake))
    assert (await check(None)).result == OK  # type: ignore[arg-type]
    await fake.stop()
    os.unlink(fake.path)
    out = await check(None)  # type: ignore[arg-type]
    assert out.result == MISSING and out.reason_code == "parser_unavailable"
    reg = ItemRegistry()
    register_parser_check(client_for(fake), reg)
    assert reg.names() == ["parser_sidecar_running"]


async def test_a_peer_reset_before_any_reply_is_unavailable_not_corrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar killed before replying is seen as EOF on macOS and as ECONNRESET on Linux. Both
    must be ``ParserUnavailableError`` (the pre-flight rolls back), never a CORRUPT file."""

    class Resetting:
        async def readexactly(self, n: int) -> bytes:
            raise ConnectionResetError(104, "Connection reset by peer")

    class Writer:
        def write(self, _b: bytes) -> None: ...
        async def drain(self) -> None: ...
        def close(self) -> None: ...
        async def wait_closed(self) -> None: ...

    async def connect(_path: str) -> tuple[Resetting, Writer]:
        return Resetting(), Writer()

    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    with pytest.raises(ParserUnavailableError, match=r"^reset$"):
        await ParserClient("/nonexistent.sock").sanitize_html(b"<p>x</p>")
