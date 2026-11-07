"""Origins of the discussion UI and of the Moodle sites (ARCH-076, plan 9).

The iframe is safe only because it runs on an origin distinct from Moodle's: the plugin sets
``allow-same-origin`` on the sandbox, which would hand the Moodle page's script the iframe's DOM
(and its token) if the two origins were the same.

``bare_origin`` is THE normalisation: revision 0033's backfill, the UI router, the API startup
refusal, the setup check and ``VEKTRA_ASSESS_UI_ORIGIN`` all go through it, so two spellings of
one origin (case, a default port, a trailing dot or slash, an IPv6 literal written long) can
never compare as different. ``ui_origin_problem`` is the one decision used by API startup and by
the setup check item ``ui_origin_distinct``.

Pure standard library: the worker (setup check) and the migration import it.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

_DEFAULT_PORT = {"http": 80, "https": 443}
#: a DNS host name: letters, digits and hyphens per label (RFC 1123); no underscore
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: the shape every normalised origin has; revision 0033's CHECK carries the same expression
ORIGIN_RE = r"^https?://([a-z0-9.\-]+|\[[0-9a-f:.]+\])(:[0-9]{1,5})?$"


def _host(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    name = host.lower().rstrip(".")
    labels = name.split(".")
    if not name or len(name) > 253 or not all(_LABEL.match(label) for label in labels):
        raise ValueError("an origin's host is a DNS name or an IP literal")
    return name


def bare_origin(value: str) -> str:
    """The normalised origin ``scheme://host[:port]``: lower-case scheme and host, no default
    port, IPv6 compressed in brackets. ValueError if ``value`` is not a bare http(s) origin
    (path, query, fragment, credentials, wildcard, a bad host or port)."""
    raw = value.strip()
    if raw.endswith("/"):
        raw = raw[:-1]
    if "*" in raw:
        raise ValueError("an origin has no wildcard")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORT or not parts.hostname:
        raise ValueError("an origin is http(s)://host[:port]")
    if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("an origin has no path, query, fragment or credentials")
    port = parts.port  # ValueError when out of range
    shown = "" if port is None or port == _DEFAULT_PORT[scheme] else f":{port}"
    return f"{scheme}://{_host(parts.hostname)}{shown}"


def url_origin(url: str) -> str | None:
    """The normalised origin of an absolute http(s) URL (its scheme and authority), or None
    when it has none that ``bare_origin`` accepts (credentials, a bad host...)."""
    try:
        parts = urlsplit(url.strip())
        return bare_origin(f"{parts.scheme}://{parts.netloc}")
    except ValueError:
        return None


def site_origin(moodle_origin: str | None, base_url: str | None) -> str | None:
    """A site's Moodle origin: its ``moodle_origin`` column, else the origin of its
    ``base_url``; normalised, or None when neither is a usable origin."""
    if moodle_origin:
        try:
            return bare_origin(moodle_origin)
        except ValueError:
            return None
    return url_origin(base_url) if base_url else None


def site_origin_or_problem(
    moodle_origin: str | None, base_url: str | None
) -> tuple[str | None, str | None]:
    """``(origin, None)``, or ``(None, "moodle_origin_invalid")`` when the site has no usable
    origin (a stored ``moodle_origin`` that is not a bare origin, or no column and a
    ``base_url`` without one): reported by startup and the setup check, never skipped
    (CodeRabbit 4501070007; the page answers 404 for such a site)."""
    origin = site_origin(moodle_origin, base_url)
    return (origin, None) if origin else (None, "moodle_origin_invalid")


#: the enabled sites' two origin columns (``site_origin`` decides in Python, not in SQL)
SITE_ORIGINS_SQL = (
    "SELECT moodle_origin, base_url FROM assess_sites WHERE disabled_at IS NULL"
)


def ui_origin_problem(
    ui_origin: str | None, moodle_origins: Iterable[str], *, development: bool
) -> str | None:
    """Why this UI origin is not acceptable (a reason code), or None when it is."""
    if not ui_origin:
        return "ui_origin_unset"
    try:
        ui = bare_origin(ui_origin)
    except ValueError:
        return "ui_origin_invalid"
    if not ui.startswith("https://") and not development:
        return "ui_origin_not_https"
    for raw in moodle_origins:
        try:
            moodle = bare_origin(raw)
        except ValueError:
            continue
        if not moodle.startswith("https://") and not development:
            return "moodle_origin_not_https"
        if moodle == ui:
            return "ui_origin_equals_moodle_origin"
    return None
