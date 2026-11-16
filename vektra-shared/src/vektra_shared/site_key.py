"""The key of an LMS site, for the ``lms_site`` chunk metadata (FEAT-028 review 1).

Pure, no import beyond the standard library: the ingest side (its pipeline sends the value) and
the assessment vertical (it filters on it) must agree byte for byte, so the rule lives here once.
See docs/reference/api.md, "Keys read by the assessment discussion".
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def lms_site_key(base_url: str | None) -> str | None:
    """The key of an LMS site: its root URL, normalised. THE one rule for both sides of the
    ``lms_site`` chunk metadata (FEAT-028): the ingest normalises the LMS root it reads from,
    the discussion normalises the site's registered ``base_url``, and a chunk is retrieved only
    when the two are equal (a namespace and a cmid are unique within ONE site only).

    Normalised as:
    ``scheme://host[:port][/path]``, scheme and host lowercased, a default port (80 / 443)
    dropped, no trailing ``/``, no query, no fragment, and any userinfo (``user:pw@``)
    removed. None when it is not an absolute http(s) URL with a host: the site is then not
    usable for course material (no fallback)."""
    if not base_url:
        return None
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None  # only http(s), with a host: anything else is not a usable site
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"
