"""Moodle web-service error envelopes (plan assess-moodle-ws, group B).

Moodle answers a failed call with HTTP 200 and a JSON object
``{"exception": ..., "errorcode": ..., "message": ..., "debuginfo": ...}``. It must be
detected BEFORE any model parsing (it is never parsed as data), and only the ``errorcode``
survives: ``message`` and ``debuginfo`` can echo user data, so they are dropped here.
"""

from __future__ import annotations

import re
from typing import Any

from vektra_assess.moodle.result import Unreadable, UnreadableReason

_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

#: errorcodes that mean "the service account may not do this" (verified on 4.3 and 5.2:
#: a function outside the service answers ``accessexception``).
_ACCESS = frozenset(
    {
        "accessexception",
        "nopermissions",
        "invalidtoken",
        "wsaccessusersuspended",
        "wsaccessuserexpired",
        "wsaccessusernologin",
        "wsaccessuserdeleted",
        "servicenotavailable",
        "forbiddenwsfunction",
        "restrictedusers",
        "missingcapability",
        "requireloginerror",
    }
)
_NOT_IN_SERVICE = frozenset(
    {"invalidrecord_function", "functionnotfound", "unknownfunction"}
)


def is_error_envelope(doc: Any) -> bool:
    return isinstance(doc, dict) and "exception" in doc and "errorcode" in doc


def error_code(doc: dict[str, Any]) -> str:
    """The sanitised ``errorcode``; anything that is not a short code becomes ``malformed``."""
    code = doc.get("errorcode")
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "malformed"


def unreadable_from_envelope(doc: dict[str, Any]) -> Unreadable:
    code = error_code(doc)
    if code in _NOT_IN_SERVICE:
        return Unreadable(UnreadableReason.FUNCTION_NOT_IN_SERVICE, code)
    if code in _ACCESS:
        return Unreadable(UnreadableReason.ACCESS_EXCEPTION, code)
    return Unreadable(UnreadableReason.UNKNOWN_SHAPE, code)
