"""Pre-parse guards for ZIP containers (DOCX, ODT): REQ-101, ARCH-066.

Nothing here trusts a declared size: entries are read as a stream that stops at the cap
whatever the header says. Nested archives are never opened.
"""

from __future__ import annotations

import io
import itertools
import struct
import zipfile
import zlib
from dataclasses import dataclass
from typing import IO

MAX_ENTRIES = 2_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RATIO = 100
_RATIO_FLOOR = 64 * 1024  # a tiny entry may compress 1000:1 legitimately


class ZipRefused(Exception):
    """The container breaks a guard: the file is CORRUPT (code says which guard)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Budget:
    remaining: int = MAX_TOTAL_BYTES


class _Capped(io.RawIOBase):
    """A read-only stream over one entry that counts every byte against the budget."""

    def __init__(self, raw: IO[bytes], budget: Budget, cap: int) -> None:
        self._raw, self._budget, self._left = raw, budget, cap

    def readable(self) -> bool:
        return True

    def readinto(self, buf: bytearray | memoryview) -> int:  # type: ignore[override]
        want = len(buf)
        if want == 0:
            return 0
        try:
            chunk = self._raw.read(want)
        except zipfile.BadZipFile as exc:
            raise ZipRefused(
                "zip_crc" if "CRC" in str(exc) else "zip_stream_error"
            ) from None
        except (EOFError, OSError, zlib.error):
            raise ZipRefused("zip_stream_error") from None
        n = len(chunk)
        self._left -= n
        self._budget.remaining -= n
        if self._left < 0 or self._budget.remaining < 0:
            raise ZipRefused("zip_stream_too_large")
        buf[:n] = chunk
        return n

    def close(self) -> None:
        self._raw.close()
        super().close()


class GuardedZip:
    def __init__(self, zf: zipfile.ZipFile, budget: Budget) -> None:
        self.zf, self.budget = zf, budget
        self.names = {i.filename for i in zf.infolist()}

    def is_encrypted(self) -> bool:
        return any(i.flag_bits & 0x1 for i in self.zf.infolist())

    def open(self, name: str, cap: int) -> io.BufferedReader:
        try:
            raw = self.zf.open(name)
        except (KeyError, zipfile.BadZipFile, NotImplementedError, RuntimeError):
            raise ZipRefused("zip_entry_unreadable") from None
        return io.BufferedReader(_Capped(raw, self.budget, cap))

    def read(self, name: str, cap: int) -> bytes:
        with self.open(name, cap) as fh:
            return fh.read()


def _local_end(data: bytes, info: zipfile.ZipInfo) -> int:
    """End of an entry's data: its LOCAL header (30 bytes plus the local filename and extra
    field, whose lengths are in the local header and may differ from the central
    directory's) plus the compressed size."""
    off = info.header_offset
    if off < 0 or off + 30 > len(data):
        raise ZipRefused("zip_unreadable")
    name_len, extra_len = struct.unpack("<HH", data[off + 26 : off + 30])
    return int(off + 30 + name_len + extra_len + info.compress_size)


def open_checked(
    data: bytes,
    *,
    max_entries: int = MAX_ENTRIES,
    max_total: int = MAX_TOTAL_BYTES,
    max_ratio: int = MAX_RATIO,
) -> GuardedZip:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, OverflowError):
        raise ZipRefused("zip_unreadable") from None
    infos = zf.infolist()
    if len(infos) > max_entries:
        raise ZipRefused("zip_too_many_entries")
    names = [i.filename for i in infos]
    if len(set(names)) != len(names):
        raise ZipRefused("zip_duplicate_entries")
    if sum(i.file_size for i in infos) > max_total:
        raise ZipRefused("zip_declared_too_large")
    for i in infos:
        if i.file_size > _RATIO_FLOOR and i.file_size > max_ratio * max(
            i.compress_size, 1
        ):
            raise ZipRefused("zip_ratio")
    ordered = sorted(infos, key=lambda i: i.header_offset)
    for cur, nxt in itertools.pairwise(ordered):
        if _local_end(data, cur) > nxt.header_offset:
            raise ZipRefused("zip_overlap")
    return GuardedZip(zf, Budget(max_total))
