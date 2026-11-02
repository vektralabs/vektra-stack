"""Wire protocol and status vocabulary of the parser sidecar (COMP-008, REQ-101).

Stdlib only: imported by both sides of the socket (the sidecar and the worker client),
so it must pull nothing that the sidecar is forbidden to hold.

Framing, one request per connection: 4-byte big-endian header length (at most
``MAX_HEADER``), a UTF-8 JSON object header, then ``payload_len`` payload bytes. The
original filename is never sent.

Owning plan: .s2s/plans/20261101-154950-assess-parser.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1
MAX_HEADER = 4096
_LEN = struct.Struct(">I")

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_CHARS = 200_000
DEFAULT_TIMEOUT_MS = 10_000

OPS = ("parse", "sanitize_html", "ping")
TRANSPORT_BUSY = "BUSY"


class FrameError(ValueError):
    """A frame that violates the protocol (short read, oversize, not JSON...)."""


class FileStatus(str, Enum):
    """Exactly one per file (REQ-101)."""

    READ = "READ"
    READ_PARTIAL = "READ_PARTIAL"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    TOO_LARGE = "TOO_LARGE"
    NO_TEXT = "NO_TEXT"
    PROTECTED = "PROTECTED"
    CORRUPT = "CORRUPT"
    OVER_MAX_FILES = "OVER_MAX_FILES"


_LABELS: dict[FileStatus, str] = {
    FileStatus.READ: "letto",
    FileStatus.READ_PARTIAL: "letto parzialmente",
    FileStatus.UNSUPPORTED_FORMAT: "non letto: formato non supportato",
    FileStatus.TOO_LARGE: "non letto: troppo grande",
    FileStatus.NO_TEXT: "non letto: testo non estraibile",
    FileStatus.PROTECTED: "non letto: protetto",
    FileStatus.CORRUPT: "non letto: file danneggiato o non elaborabile",
    FileStatus.OVER_MAX_FILES: "non letto: oltre il numero massimo di file",
}


def label(status: FileStatus, upto_page: int | None = None) -> str:
    """The canonical Italian label. ``READ_PARTIAL`` names the page when there is one."""
    if status is FileStatus.READ_PARTIAL and upto_page is not None:
        return f"letto parzialmente (fino a pagina {upto_page})"
    return _LABELS[status]


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_pages: int = DEFAULT_MAX_PAGES
    max_chars: int = DEFAULT_MAX_CHARS
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    def to_wire(self) -> dict[str, int]:
        return {
            "max_pages": self.max_pages,
            "max_chars": self.max_chars,
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True)
class Extracted:
    """What an extractor returns: text plus exactly one status."""

    status: FileStatus
    text: str = ""
    upto_page: int | None = None
    pages_total: int | None = None
    code: str = ""


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    head = {**header, "payload_len": len(payload)}
    raw = json.dumps(head, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER:
        raise FrameError(f"header of {len(raw)} bytes exceeds {MAX_HEADER}")
    return _LEN.pack(len(raw)) + raw + payload


def parse_header_length(prefix: bytes) -> int:
    if len(prefix) != _LEN.size:
        raise FrameError("short read of the header length")
    (n,) = _LEN.unpack(prefix)
    if n == 0 or n > MAX_HEADER:
        raise FrameError(f"header length {n} outside 1..{MAX_HEADER}")
    return int(n)


def parse_header(raw: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FrameError("header is not valid UTF-8 JSON") from None
    if not isinstance(doc, dict):
        raise FrameError("header is not a JSON object")
    n = doc.get("payload_len")
    if type(n) is not int or n < 0:
        raise FrameError("payload_len missing, negative or not an integer")
    return doc


async def read_frame(
    reader: asyncio.StreamReader, *, max_payload: int
) -> tuple[dict[str, Any], bytes]:
    """Read one frame; ``FrameError`` on any violation, nothing is trusted."""
    try:
        n = parse_header_length(await reader.readexactly(_LEN.size))
        header = parse_header(await reader.readexactly(n))
        if header["payload_len"] > max_payload:
            raise FrameError(
                f"payload of {header['payload_len']} bytes exceeds {max_payload}"
            )
        payload = await reader.readexactly(header["payload_len"])
    except asyncio.IncompleteReadError:
        raise FrameError("truncated frame") from None
    return header, payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(min(65536, n - got))
        if not chunk:
            raise FrameError("truncated frame")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame_sync(
    sock: socket.socket, *, max_payload: int
) -> tuple[dict[str, Any], bytes]:
    n = parse_header_length(_recv_exact(sock, _LEN.size))
    header = parse_header(_recv_exact(sock, n))
    if header["payload_len"] > max_payload:
        raise FrameError(
            f"payload of {header['payload_len']} bytes exceeds {max_payload}"
        )
    return header, _recv_exact(sock, header["payload_len"])


def new_req_id() -> str:
    return os.urandom(8).hex()
