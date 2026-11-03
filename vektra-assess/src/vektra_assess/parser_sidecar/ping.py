"""Healthcheck client of the sidecar: a framed ping over the socket (exit non-zero on failure).

``--deep`` makes the sidecar fork one child under rlimits on an embedded one-page PDF,
so a green check proves the pool works and not only that the socket answers.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

from vektra_assess.parser_protocol import (
    PROTOCOL_VERSION,
    FrameError,
    encode_frame,
    new_req_id,
    read_frame_sync,
)

DEFAULT_SOCKET = "/run/vektra-assess-parser/parser.sock"
PING_TEXT = "Vektra assess parser self-check page. " * 4  # above the scanned threshold


def ping_pdf() -> bytes:
    """The embedded one-page fixture of the deep ping."""
    content = f"BT /F1 12 Tf 40 700 Td ({PING_TEXT}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def ping(path: str, *, deep: bool, timeout_s: float) -> float:
    """Round-trip in seconds, or raises (OSError, FrameError, RuntimeError)."""
    t0 = time.monotonic()
    req_id = new_req_id()
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout_s)
        s.connect(path)
        s.sendall(
            encode_frame(
                {"v": PROTOCOL_VERSION, "op": "ping", "req_id": req_id, "deep": deep}
            )
        )
        header, _payload = read_frame_sync(s, max_payload=4096)
    if header.get("req_id") != req_id or header.get("code") != "pong":
        raise RuntimeError(f"unexpected ping reply: {header.get('code')!r}")
    if deep and header.get("status") != "READ":
        raise RuntimeError(f"deep ping parsed to {header.get('status')!r}")
    return time.monotonic() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vektra_assess.parser_sidecar.ping")
    ap.add_argument("--deep", action="store_true")
    ap.add_argument(
        "--socket",
        default=os.environ.get("VEKTRA_ASSESS_PARSER_SOCKET", DEFAULT_SOCKET),
    )
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args(argv)
    try:
        ping(str(Path(args.socket)), deep=args.deep, timeout_s=args.timeout)
    except (OSError, FrameError, RuntimeError) as exc:
        print(f"ping failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
