"""The per-item child: runs in a fresh process, one item, never reused (NFR-014).

Order matters: before the child reads a single input byte it has replaced stdout and
stderr with a pipe the parent drains and discards, closed every inherited descriptor,
dropped core dumps and tracing, and lowered its resource limits. A parse that blows a
limit dies here, not in the supervisor.
"""

from __future__ import annotations

import json
import logging
import os
import warnings
from multiprocessing.connection import Connection
from typing import Any

from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_assess.parser_sidecar.docx import extract_docx
from vektra_assess.parser_sidecar.html_review import (
    extract_review_item,
    sanitize_review_html,
)
from vektra_assess.parser_sidecar.odt import extract_odt
from vektra_assess.parser_sidecar.pdf import extract_pdf
from vektra_assess.parser_sidecar.sniff import Kind, sniff
from vektra_assess.parser_sidecar.text import extract_text

PRELOAD = ("vektra_assess.parser_sidecar.child",)
FD_LIMIT = 32
PR_SET_DUMPABLE = 4


def run(
    op: str, data: bytes, ext_hint: str, *, max_pages: int, max_chars: int
) -> Extracted:
    """Pure dispatch (no process hygiene): what the child computes for one item."""
    try:
        if op == "sanitize_html":
            return sanitize_review_html(data, max_chars)
        if op == "review_item":
            return extract_review_item(data, max_chars)
        sn = sniff(data, ext_hint)
        if sn.kind is Kind.REFUSED:
            assert sn.status is not None
            return Extracted(sn.status, code=sn.code)
        if sn.kind is Kind.PDF:
            return extract_pdf(data, max_pages, max_chars)
        if sn.kind is Kind.DOCX:
            return extract_docx(data, max_chars)
        if sn.kind is Kind.ODT:
            return extract_odt(data, max_chars)
        return extract_text(data, max_chars)
    except MemoryError:
        return Extracted(FileStatus.CORRUPT, code="memory")
    except RecursionError:
        return Extracted(FileStatus.CORRUPT, code="recursion")
    except Exception:
        return Extracted(FileStatus.CORRUPT, code="parse_error")


def _peaks() -> dict[str, int | None]:
    """Peak virtual and resident size of this child (Linux /proc), for the sizing data."""
    out: dict[str, int | None] = {"vm_peak_kb": None, "rss_peak_kb": None}
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmPeak:"):
                    out["vm_peak_kb"] = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    out["rss_peak_kb"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return out


def serialise(res: Extracted, max_chars: int) -> bytes:
    text = res.text[:max_chars]
    head = {
        **_peaks(),
        "status": res.status.value,
        "upto_page": res.upto_page,
        "pages_total": res.pages_total,
        "code": res.code,
        "chars": len(text),
    }
    return json.dumps(head).encode() + b"\n" + text.encode("utf-8", "replace")


def _close_inherited(keep: set[int]) -> None:
    try:
        fds = [int(n) for n in os.listdir("/dev/fd")]
    except OSError:
        fds = list(range(3, 256))
    for fd in fds:
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def _limits(spec: dict[str, Any]) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - not POSIX
        return
    timeout_s = max(1, int(spec["timeout_ms"]) // 1000 + 1)
    wanted: list[tuple[int, int]] = [
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, timeout_s + 1),
        (resource.RLIMIT_NOFILE, FD_LIMIT),
        (resource.RLIMIT_FSIZE, 0),
    ]
    if hasattr(resource, "RLIMIT_AS") and int(spec["rlimit_as"]) > 0:
        wanted.append((resource.RLIMIT_AS, int(spec["rlimit_as"])))
    for res, val in wanted:
        try:
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):  # e.g. RLIMIT_AS is not enforced on this OS
            pass


def _no_dump() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def child_main(
    in_conn: Connection, out_conn: Connection, err_w: Connection, spec: dict[str, Any]
) -> None:
    """Entry of the child process. Never returns normally; always ``os._exit``."""
    code = 70
    try:
        os.dup2(err_w.fileno(), 1)
        os.dup2(err_w.fileno(), 2)
        _close_inherited({in_conn.fileno(), out_conn.fileno()})
        logging.disable(logging.CRITICAL)
        warnings.simplefilter("ignore")
        _no_dump()
        _limits(spec)
        data = in_conn.recv_bytes(int(spec["max_file_bytes"]) + 1)
        res = run(
            spec["op"],
            data,
            spec["ext_hint"],
            max_pages=int(spec["max_pages"]),
            max_chars=int(spec["max_chars"]),
        )
        out_conn.send_bytes(serialise(res, int(spec["max_chars"])))
        code = 0
    except BaseException:
        code = 70
    finally:
        os._exit(code)
