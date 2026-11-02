"""Framing, labels and limits of the parser protocol (plan assess-parser, task A)."""

from __future__ import annotations

import asyncio
import json
import struct

import pytest

from vektra_assess.parser_protocol import (
    DEFAULT_MAX_CHARS,
    MAX_HEADER,
    FileStatus,
    FrameError,
    Limits,
    encode_frame,
    label,
    parse_header,
    parse_header_length,
    read_frame,
)

LABELS = {
    FileStatus.READ: "letto",
    FileStatus.READ_PARTIAL: "letto parzialmente",
    FileStatus.UNSUPPORTED_FORMAT: "non letto: formato non supportato",
    FileStatus.TOO_LARGE: "non letto: troppo grande",
    FileStatus.NO_TEXT: "non letto: testo non estraibile",
    FileStatus.PROTECTED: "non letto: protetto",
    FileStatus.CORRUPT: "non letto: file danneggiato o non elaborabile",
    FileStatus.OVER_MAX_FILES: "non letto: oltre il numero massimo di file",
}


@pytest.mark.parametrize("status", list(FileStatus))
def test_label_is_byte_exact(status: FileStatus) -> None:
    assert label(status).encode("utf-8") == LABELS[status].encode("utf-8")


def test_every_status_has_a_label_and_the_vocabulary_is_closed() -> None:
    assert set(LABELS) == set(FileStatus) and len(FileStatus) == 8


def test_partial_label_names_the_page() -> None:
    assert label(FileStatus.READ_PARTIAL, 50) == "letto parzialmente (fino a pagina 50)"
    assert label(FileStatus.READ_PARTIAL, None) == "letto parzialmente"
    assert label(FileStatus.READ, 3) == "letto"


def test_limits_defaults() -> None:
    lim = Limits()
    assert (lim.max_file_bytes, lim.max_pages, lim.max_chars) == (
        10 * 1024 * 1024,
        50,
        DEFAULT_MAX_CHARS,
    )
    assert DEFAULT_MAX_CHARS == 200_000


async def _reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


async def test_roundtrip() -> None:
    frame = encode_frame({"v": 1, "op": "ping"}, b"abc")
    header, payload = await read_frame(await _reader(frame), max_payload=10)
    assert header["op"] == "ping" and header["payload_len"] == 3 and payload == b"abc"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00\x00",  # short length prefix
        struct.pack(">I", 0),  # zero-length header
        struct.pack(">I", MAX_HEADER + 1) + b"x" * 10,  # oversize header
        struct.pack(">I", 0xFFFFFFFF),  # "negative" (huge) length
        struct.pack(">I", 5) + b"{}",  # truncated header
        struct.pack(">I", 3) + b"abc",  # not JSON
        struct.pack(">I", 2) + b"[]",  # not an object
        struct.pack(">I", 2) + b"\xff\xfe",  # not UTF-8
        struct.pack(">I", 2) + b"{}",  # no payload_len
    ],
)
async def test_malformed_frames_are_rejected(data: bytes) -> None:
    with pytest.raises(FrameError):
        await read_frame(await _reader(data), max_payload=100)


async def test_truncated_and_oversize_and_negative_payload() -> None:
    head = json.dumps({"payload_len": 10}).encode()
    with pytest.raises(FrameError, match="truncated"):
        await read_frame(
            await _reader(struct.pack(">I", len(head)) + head + b"123"), max_payload=100
        )
    with pytest.raises(FrameError, match="exceeds"):
        await read_frame(
            await _reader(struct.pack(">I", len(head)) + head), max_payload=5
        )
    neg = json.dumps({"payload_len": -1}).encode()
    with pytest.raises(FrameError, match="payload_len"):
        await read_frame(
            await _reader(struct.pack(">I", len(neg)) + neg), max_payload=5
        )
    boolean = json.dumps({"payload_len": True}).encode()
    with pytest.raises(FrameError, match="payload_len"):
        parse_header(boolean)


def test_encode_refuses_oversize_header() -> None:
    with pytest.raises(FrameError):
        encode_frame({"x": "a" * MAX_HEADER})


def test_header_length_helper() -> None:
    assert parse_header_length(struct.pack(">I", 10)) == 10
    with pytest.raises(FrameError):
        parse_header_length(b"\x00")
