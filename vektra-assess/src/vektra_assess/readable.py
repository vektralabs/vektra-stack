"""Typed outcome of every external read: ``Readable(value) | Unreadable(reason)`` (ARCH-070).

No ``None`` and no exception escapes a reader. Consumers must pattern-match; with
``assert_never`` in the default branch mypy enforces exhaustiveness, so a new
``UnreadableReason`` cannot be silently ignored. Every unknown defaults to Unreadable,
which fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class UnreadableReason(StrEnum):
    FUNCTION_NOT_IN_SERVICE = "function_not_in_service"
    ACCESS_EXCEPTION = "access_exception"
    UNKNOWN_SHAPE = "unknown_shape"
    PARTIAL = "partial"
    BREAKER_OPEN = "breaker_open"
    TIMEOUT = "timeout"
    SIZE_CAP = "size_cap"
    TLS = "tls"
    REDIRECT = "redirect"
    VERSION_UNSUPPORTED = "version_unsupported"
    NOT_PROBED = "not_probed"
    RELEASE_CHANGED = "release_changed"


@dataclass(frozen=True)
class ShapeEvidence:
    """Content-free description of what a call returned: never a value, never a Moodle id."""

    wsfunction: str
    release: str | None
    signature: str  # sha256 over the canonical shape signature (keys, types, counts)


@dataclass(frozen=True)
class Readable[T]:
    value: T
    evidence: ShapeEvidence | None = None


@dataclass(frozen=True)
class Unreadable:
    reason: UnreadableReason
    #: a short machine code (a Moodle ``errorcode``, an HTTP status class...): never a message
    detail_code: str = ""

    def __post_init__(self) -> None:
        if len(self.detail_code) > 64 or not all(
            c.isalnum() or c in "_-." for c in self.detail_code
        ):
            raise ValueError("detail_code must be a short [A-Za-z0-9_.-] code")


type Reading[T] = Readable[T] | Unreadable
