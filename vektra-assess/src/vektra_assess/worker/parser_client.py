"""Worker-side client of the network-less parser sidecar (COMP-008, INT-003).

Imported only by ``vektra_assess.worker.*`` (import-linter). Every byte that comes back
is hostile: the header is bounded and schema-checked, ``req_id`` must match, the status
must be in the REQ-101 vocabulary (and not the caller-only ``OVER_MAX_FILES``), the
payload is capped, strictly UTF-8, stripped of control characters and its length must
match what the header claims. Any violation becomes ``CORRUPT`` for that file plus a
content-free log code; it never reaches the caller as text.

If the sidecar is not there (socket missing, refused, no answer in time, ``BUSY`` after
bounded retries) the feature is unavailable: ``ParserUnavailableError``. There is no
in-process fallback, ever (ARCH-066).

Owning plan: .s2s/plans/20261101-154950-assess-parser.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle.setup_check import (
    DEFAULT_REGISTRY,
    CheckContext,
    ItemRegistry,
    Outcome,
    missing,
    ok,
)
from vektra_assess.parser_protocol import (
    MAX_HEADER,
    OPS,
    PROTOCOL_VERSION,
    TRANSPORT_BUSY,
    FileStatus,
    FrameError,
    Limits,
    PlainText,
    encode_frame,
    new_req_id,
    parse_header,
    parse_header_length,
)
from vektra_assess.parser_protocol import label as status_label

log = structlog.get_logger(__name__)

PROTOCOL_VIOLATION_CODE = "assess_parser_protocol_violation"
_CTRL = {c: None for c in [*range(0, 9), *range(11, 32), 127]}  # keep \t and \n
_TEXT_STATUSES = frozenset({FileStatus.READ, FileStatus.READ_PARTIAL})
_QUEUE_ALLOWANCE_S = 15.0


class ParserUnavailableError(Exception):
    """The sidecar cannot serve: the pre-flight handler rolls back (REQ-071)."""


class _Violation(Exception):
    pass


class _Unavailable(Exception):
    pass


@dataclass(frozen=True)
class ParseResult:
    status: FileStatus
    text: str = ""
    upto_page: int | None = None
    pages_total: int | None = None
    chars: int = 0
    code: str = ""

    @property
    def label(self) -> str:
        return status_label(self.status, self.upto_page)


_EMPTY = PlainText("")


@dataclass(frozen=True)
class SanitizeResult:
    """A sanitised quiz review page. ``text`` is ``PlainText``: markup-like characters in
    it are text, and every render site must escape it (``parser_protocol.html_escape``)."""

    status: FileStatus
    text: PlainText = _EMPTY
    upto_page: int | None = None
    pages_total: int | None = None
    chars: int = 0
    code: str = ""

    @property
    def label(self) -> str:
        return status_label(self.status, self.upto_page)

    @classmethod
    def of(cls, r: ParseResult) -> SanitizeResult:
        return cls(
            r.status, PlainText(r.text), r.upto_page, r.pages_total, r.chars, r.code
        )


def _violation_result(reason: str) -> ParseResult:
    # a fixed reason code, never content
    log.warning(PROTOCOL_VIOLATION_CODE, reason_code=reason)
    return ParseResult(FileStatus.CORRUPT, code="protocol_violation")


class ParserClient:
    def __init__(
        self,
        socket_path: Path | str,
        *,
        limits: Limits | None = None,
        queue_allowance_s: float = _QUEUE_ALLOWANCE_S,
        connect_timeout_s: float = 3.0,
        busy_retries: int = 8,
        busy_backoff_s: float = 0.25,
    ) -> None:
        self.socket_path = str(socket_path)
        self.limits = limits or Limits()
        self.queue_allowance_s = queue_allowance_s
        self.connect_timeout_s = connect_timeout_s
        self.busy_retries = busy_retries
        self.busy_backoff_s = busy_backoff_s

    @classmethod
    def from_settings(cls, settings: VektraAssessSettings) -> ParserClient:
        return cls(
            settings.parser_socket,
            limits=Limits(
                max_chars=settings.parser_max_chars,
                timeout_ms=int(settings.parser_timeout_s * 1000),
            ),
        )

    # --- public API -------------------------------------------------------------

    async def parse_file(
        self, data: bytes, ext_hint: str = "", limits: Limits | None = None
    ) -> ParseResult:
        return await self._call("parse", data, ext_hint, limits or self.limits)

    async def sanitize_html(
        self, html: bytes, limits: Limits | None = None
    ) -> SanitizeResult:
        return SanitizeResult.of(
            await self._call("sanitize_html", html, "html", limits or self.limits)
        )

    async def ping(self, deep: bool = False) -> float:
        """Round-trip in seconds. ``ParserUnavailableError`` unless the sidecar answers
        (a deep ping also needs its pool to parse the embedded fixture)."""
        t0 = time.monotonic()
        req_id = new_req_id()
        budget = (20.0 if deep else 5.0) + self.connect_timeout_s
        try:
            header, _ = await asyncio.wait_for(
                self._exchange(
                    {"op": "ping", "req_id": req_id, "deep": deep}, b"", max_payload=0
                ),
                budget,
            )
        except (_Unavailable, _Violation, TimeoutError, OSError) as exc:
            raise ParserUnavailableError(type(exc).__name__) from None
        if header.get("req_id") != req_id or header.get("code") != "pong":
            raise ParserUnavailableError("bad_pong")
        if deep and header.get("status") != FileStatus.READ.value:
            raise ParserUnavailableError("deep_ping_failed")
        return time.monotonic() - t0

    # --- internals ----------------------------------------------------------------

    async def _call(
        self, op: str, data: bytes, ext_hint: str, limits: Limits
    ) -> ParseResult:
        if op not in OPS:  # pragma: no cover - internal misuse
            raise ValueError(op)
        budget = limits.timeout_ms / 1000 + self.queue_allowance_s
        deadline = time.monotonic() + budget
        for attempt in range(self.busy_retries + 1):
            req_id = new_req_id()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParserUnavailableError("client_timeout")
            try:
                header, payload = await asyncio.wait_for(
                    self._exchange(
                        {
                            "op": op,
                            "req_id": req_id,
                            "ext_hint": ext_hint,
                            "limits": limits.to_wire(),
                        },
                        data,
                        max_payload=limits.max_chars * 4,
                    ),
                    remaining,
                )
            except TimeoutError:
                raise ParserUnavailableError("client_timeout") from None
            except _Unavailable as exc:
                raise ParserUnavailableError(str(exc)) from None
            except _Violation as exc:
                return _violation_result(str(exc))
            if header.get("code") == TRANSPORT_BUSY and header.get("status") is None:
                if attempt < self.busy_retries:
                    await asyncio.sleep(self.busy_backoff_s * (attempt + 1))
                    continue
                raise ParserUnavailableError("busy")
            try:
                return self._validate(header, payload, req_id, limits)
            except _Violation as exc:
                return _violation_result(str(exc))
        raise ParserUnavailableError("busy")  # pragma: no cover

    async def _exchange(
        self, header: dict[str, Any], payload: bytes, *, max_payload: int
    ) -> tuple[dict[str, Any], bytes]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), self.connect_timeout_s
            )
        except (OSError, TimeoutError) as exc:
            raise _Unavailable(type(exc).__name__) from None
        try:
            try:
                writer.write(encode_frame({"v": PROTOCOL_VERSION, **header}, payload))
                await writer.drain()
            except (ConnectionError, OSError):
                pass  # the sidecar may answer (e.g. TOO_LARGE) and close before reading it all
            return await self._read_response(reader, max_payload)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _read_response(
        reader: asyncio.StreamReader, max_payload: int
    ) -> tuple[dict[str, Any], bytes]:
        try:
            prefix = await reader.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                raise _Unavailable("closed_without_reply") from None
            raise _Violation("truncated_prefix") from None
        except (ConnectionError, OSError):
            raise _Unavailable("reset") from None
        try:
            n = parse_header_length(prefix)
            if n > MAX_HEADER:  # parse_header_length already refuses; belt and braces
                raise FrameError("header too large")
            header = parse_header(await reader.readexactly(n))
            plen = header["payload_len"]
            if plen > max_payload:
                raise FrameError("payload over the cap")
            payload = await reader.readexactly(plen)
        except FrameError as exc:
            raise _Violation(_reason(exc)) from None
        except asyncio.IncompleteReadError:
            raise _Violation("truncated") from None
        except (ConnectionError, OSError):
            raise _Violation("reset_mid_reply") from None
        return header, payload

    @staticmethod
    def _validate(
        header: dict[str, Any], payload: bytes, req_id: str, limits: Limits
    ) -> ParseResult:
        if header.get("v") != PROTOCOL_VERSION:
            raise _Violation("version")
        if header.get("req_id") != req_id:
            raise _Violation("req_id_mismatch")
        raw_status = header.get("status")
        if not isinstance(raw_status, str):
            raise _Violation("status_missing")
        try:
            status = FileStatus(raw_status)
        except ValueError:
            raise _Violation("status_unknown") from None
        if status is FileStatus.OVER_MAX_FILES:
            raise _Violation("status_caller_only")
        upto, total = header.get("upto_page"), header.get("pages_total")
        for v in (upto, total):
            if v is not None and (type(v) is not int or v < 0):
                raise _Violation("page_field")
        if upto is not None and not (1 <= upto <= limits.max_pages):
            raise _Violation("upto_page_range")
        if status is not FileStatus.READ_PARTIAL and upto is not None:
            raise _Violation("upto_page_status")
        code = header.get("code", "")
        if not isinstance(code, str) or len(code) > 64:
            raise _Violation("code")
        chars = header.get("chars")
        if type(chars) is not int or chars < 0:
            raise _Violation("chars_field")
        if status not in _TEXT_STATUSES and payload:
            raise _Violation("text_without_text_status")
        try:
            text = payload.decode("utf-8")  # strict: no replacement characters
        except UnicodeDecodeError:
            raise _Violation("not_utf8") from None
        if len(text) != chars:
            raise _Violation("chars_mismatch")
        if len(text) > limits.max_chars:
            raise _Violation("over_max_chars")
        text = text.translate(_CTRL)
        return ParseResult(status, text, upto, total, len(text), code)


def _reason(exc: FrameError) -> str:
    msg = str(exc)
    for key, reason in (
        ("not valid UTF-8", "header_not_json"),
        ("not a JSON object", "header_not_object"),
        ("payload_len", "payload_len"),
        ("header length", "header_length"),
        ("payload", "payload_over_cap"),
    ):
        if key in msg:
            return reason
    return "frame"


# --- setup check (REQ-079) ---------------------------------------------------------


def parser_setup_check(client: ParserClient) -> Any:
    """The ``parser_sidecar_running`` item: a deep ping, round trip measured and logged."""

    async def check(ctx: CheckContext) -> Outcome:
        try:
            rtt = await client.ping(deep=True)
        except ParserUnavailableError as exc:
            log.warning(
                "assess_parser_setup_check_failed", error_type=type(exc).__name__
            )
            return missing("parser_unavailable")
        log.info("assess_parser_setup_check", round_trip_ms=int(rtt * 1000))
        return ok()

    return check


def register_parser_check(
    client: ParserClient, registry: ItemRegistry = DEFAULT_REGISTRY
) -> None:
    registry.register("parser_sidecar_running", parser_setup_check(client))


__all__ = [
    "ParseResult",
    "ParserClient",
    "ParserUnavailableError",
    "SanitizeResult",
    "parser_setup_check",
    "register_parser_check",
]
