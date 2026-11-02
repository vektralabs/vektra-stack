"""Assess parser sidecar entry, stub (plan assess-foundations 6.3, COMP-008).

The parser parses untrusted submission files. It runs with no network, no secrets and no
DSN, and talks only over a unix socket shared with the worker. Before binding, it proves
that it holds nothing (``enforce_key_matrix("parser")``: secrets dir absent or empty, no
key or DSN env var). Plan assess-parser replaces the request handling and must keep
that check.

Framing: 4-byte big-endian length, then a UTF-8 JSON object. ``{"op": "ping"}`` is
answered ``{"ok": true, "op": "pong"}``; anything else ``{"ok": false, "error":
"not_implemented"}``. Frames above ``MAX_FRAME`` are refused and the connection closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import struct
import sys
from pathlib import Path
from typing import Any

from vektra_shared.startup import StartupValidationError

STEP = "assess_parser_check"
MAX_FRAME = 1 << 20
BODY_TIMEOUT_S = 10.0  # a peer that sends a header and stalls must not hold the task
_HEADER = struct.Struct(">I")


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise ValueError(f"frame of {length} bytes exceeds {MAX_FRAME}")
    try:
        body = await asyncio.wait_for(reader.readexactly(length), BODY_TIMEOUT_S)
    except (asyncio.IncompleteReadError, TimeoutError):
        raise ValueError("truncated or stalled frame body") from None
    try:
        doc = json.loads(body)
    except RecursionError:
        raise ValueError("frame nesting too deep") from None
    if not isinstance(doc, dict):
        raise ValueError("frame is not a JSON object")
    return doc


def encode_frame(doc: dict[str, Any]) -> bytes:
    body = json.dumps(doc).encode("utf-8")
    return _HEADER.pack(len(body)) + body


def handle(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("op") == "ping":
        return {"ok": True, "op": "pong"}
    return {"ok": False, "error": "not_implemented"}


async def _serve_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            try:
                request = await read_frame(reader)
            except (ValueError, UnicodeDecodeError):
                writer.write(encode_frame({"ok": False, "error": "bad_frame"}))
                await writer.drain()
                return
            if request is None:
                return
            writer.write(encode_frame(handle(request)))
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_server(socket_path: Path) -> asyncio.base_events.Server:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()  # stale path from a previous run
    return await asyncio.start_unix_server(_serve_client, path=str(socket_path))


def check_environment() -> Path:
    """Refuse to run unless enabled and holding nothing. Returns the socket path."""
    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.keys import enforce_key_matrix

    settings = VektraAssessSettings()
    if not settings.enabled:
        raise RuntimeError(
            "VEKTRA_ASSESS_ENABLED is not true: the assess parser does not run on a "
            "deployment where assess is disabled."
        )
    enforce_key_matrix("parser", secrets_dir=settings.secrets_dir, step=STEP)
    return settings.parser_socket


async def _amain() -> int:
    try:
        socket_path = check_environment()
    except RuntimeError as exc:
        print(f"[ASSESS PARSER] {exc}", file=sys.stderr)
        return 2
    except StartupValidationError as exc:
        print(exc.to_plain_text(), file=sys.stderr)
        return 1
    server = await start_server(socket_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
