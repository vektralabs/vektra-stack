"""Origins of the discussion UI and of the Moodle sites (ARCH-076, plan 9).

The iframe is safe only because it runs on an origin distinct from Moodle's: the plugin sets
``allow-same-origin`` on the sandbox, which would hand the Moodle page's script the iframe's DOM
(and its token) if the two origins were the same. ``bare_origin`` validates an origin as
``scheme://host[:port]`` (no path, query, fragment, credentials or wildcard); the same rules
serve ``VEKTRA_ASSESS_UI_ORIGIN`` and a site's ``moodle_origin``. ``ui_origin_problem`` is the
one decision used by API startup and by the setup check item ``ui_origin_distinct``.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


def bare_origin(value: str) -> str:
    """The normalised origin (lower-case scheme and host, no trailing slash); ValueError if
    ``value`` is not a bare http(s) origin."""
    raw = value.strip()
    if raw.endswith("/"):
        raw = raw[:-1]
    if "*" in raw:
        raise ValueError("an origin has no wildcard")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("an origin is http(s)://host[:port]")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("an origin has no path, query, fragment or credentials")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def ui_origin_problem(
    ui_origin: str | None, moodle_origins: Iterable[str], *, development: bool
) -> str | None:
    """Why this UI origin is not acceptable (a reason code), or None when it is."""
    if not ui_origin:
        return "ui_origin_unset"
    if not ui_origin.startswith("https://") and not development:
        return "ui_origin_not_https"
    for moodle in moodle_origins:
        if not moodle.startswith("https://") and not development:
            return "moodle_origin_not_https"
        if moodle == ui_origin:
            return "ui_origin_equals_moodle_origin"
    return None
