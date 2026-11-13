"""The text rules of the REQ-089 privacy settings, with no dependency (no database, no ORM).

One rule for every place a privacy text is checked: the operator's ``PUT .../privacy``, the
``notice_version`` a mint echoes (plugin request P3), the token claim that carries it, and the
deployment stand-in ``VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION`` checked at settings load. The
privacy config module (which reads the database) and ``tokens`` import it from here.

Control characters: C0 other than tab and newline, DEL (0x7f) and C1 (0x80-0x9f) are refused.
DEL and C1 were added after rows could already be stored; a row read back is checked with
``legacy=True`` (C0 only), so a stored row the earlier rule accepted stays readable and
startable, while every new write, echo and setting meets the full rule.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: field -> (minimum, maximum) length; the bounds of the stored row's CHECKs
TEXT_BOUNDS: dict[str, tuple[int, int]] = {
    "legal_basis_label": (1, 200),
    "legal_basis_text": (1, 4000),
    "notice_url": (9, 2000),
    "notice_text": (1, 8000),
    "notice_version": (1, 40),
}


def _forbidden(ch: str, *, legacy: bool) -> bool:
    code = ord(ch)
    if code < 32:
        return ch not in "\n\t"
    return not legacy and (code == 0x7F or 0x80 <= code <= 0x9F)


def has_host(url: str) -> bool:
    """A URL a browser can follow: it parses and names a host (``https:///x`` does not, and
    ``https://[x`` does not parse: never an exception out of the validation)."""
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def text_field_error(
    name: str, value: Any, *, legacy: bool = False
) -> dict[str, object] | None:
    """``{field, code}`` when one text setting (a ``TEXT_BOUNDS`` name) is invalid or missing,
    ``None`` when it is valid. ``legacy``: the read-side check of a stored row (see above)."""
    low, high = TEXT_BOUNDS[name]
    if value is None:
        return {"field": name, "code": "required"}
    if not isinstance(value, str):
        return {"field": name, "code": "not_a_string"}
    if not value.strip():
        return {"field": name, "code": "empty"}
    if len(value) < low:
        return {"field": name, "code": "too_short", "min": low}
    if len(value) > high:
        return {"field": name, "code": "too_long", "max": high}
    if name == "notice_url" and not value.startswith("https://"):
        return {"field": name, "code": "not_https"}
    if name == "notice_url" and not has_host(value):
        return {"field": name, "code": "invalid_url"}
    if any(_forbidden(ch, legacy=legacy) for ch in value):
        return {"field": name, "code": "control_character"}
    return None


def notice_version_error(value: Any) -> str | None:
    """The code refusing a ``notice_version`` (echoed, carried or configured), ``None`` if valid."""
    error = text_field_error("notice_version", value)
    return None if error is None else str(error["code"])
