"""Plumbing shared by every reader: version dispatch, strict parsing, warnings, safety net.

Rules (plan assess-moodle-ws group C):
- the model set comes from the release branch of ``SiteRef.release``; an unknown branch is
  ``Unreadable(version_unsupported)`` for every reader;
- a payload that does not fit the strict model (extra key, missing key, wrong type) is
  ``Unreadable(unknown_shape)``; the validation error is dropped, it can carry data;
- any ``warnings[]`` entry is ``Unreadable(partial)`` (spike trap 1);
- no exception escapes a reader (``@reader``); nothing is logged here but the exception class.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog
from pydantic import ValidationError

from vektra_lms_moodle.client import MoodleWsClient, SiteRef
from vektra_lms_moodle.models import SUPPORTED_BRANCHES, branch_of
from vektra_lms_moodle.result import Readable, Reading, Unreadable, UnreadableReason

log = structlog.get_logger(__name__)


def reader[T](
    fn: Callable[..., Awaitable[Reading[T]]],
) -> Callable[..., Awaitable[Reading[T]]]:
    """Safety net: a bug or an unexpected value inside a reader is Unreadable, never a raise."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Reading[T]:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            log.warning(
                "moodle_reader_error", reader=fn.__name__, error=type(exc).__name__
            )
            return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "reader_error")

    return wrapper


def check_version(site: SiteRef) -> Unreadable | str:
    branch = branch_of(site.release)
    if branch is None:
        return Unreadable(UnreadableReason.VERSION_UNSUPPORTED, "unknown_branch")
    return branch


def warning_code(doc: Any) -> str | None:
    """The first warning's code as a safe short detail, or None when there is no warning."""
    warnings = getattr(doc, "warnings", None)
    if not warnings:
        return None
    code = str(getattr(warnings[0], "warningcode", "")) or "unknown"
    return "warning_" + (code if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", code) else "x")


async def fetch(
    client: MoodleWsClient,
    site: SiteRef,
    wsfunction: str,
    params: Mapping[str, Any] | None = None,
    *,
    allow_warnings: bool = False,
) -> Reading[Any]:
    """Call one wsfunction and parse it with the release branch's strict model."""
    branch = check_version(site)
    if isinstance(branch, Unreadable):
        return branch
    adapter = SUPPORTED_BRANCHES[branch].get(wsfunction)
    if adapter is None:
        return Unreadable(UnreadableReason.FUNCTION_NOT_IN_SERVICE, "no_model")
    raw = await client.call(site, wsfunction, params)
    if isinstance(raw, Unreadable):
        return raw
    try:
        model = adapter.validate_python(raw.value)
    except ValidationError:
        return Unreadable(UnreadableReason.UNKNOWN_SHAPE, "shape_mismatch")
    if not allow_warnings:
        code = warning_code(model)
        if code is not None:
            return Unreadable(UnreadableReason.PARTIAL, code)
    return Readable(model, raw.evidence)
