"""Response headers of the discussion UI route and its assets (ARCH-076, plan 9).

The page may be framed by exactly one origin, the site's Moodle origin, read from the site record
on every request (no cache). Everything else on the page is ``'self'``; trusted types forbid any
HTML sink. There is no ``X-Frame-Options`` here: CSP ``frame-ancestors`` governs, and the API
keeps ``X-Frame-Options: DENY`` on every other route (``vektra_app.main``).
"""

from __future__ import annotations

#: the CSP of the page, with the frame-ancestors source last (asserted byte-for-byte)
CSP_BASE = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "object-src 'none'; frame-ancestors {ancestors}; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)

PAGE_BASE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": (
        "clipboard-write=(self), camera=(), microphone=(), geolocation=()"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
}

#: paths whose responses do NOT get X-Frame-Options: DENY (the page and its assets)
UI_PATH_PREFIX = "/api/v1/assess/ui"


def is_ui_path(path: str) -> bool:
    """The UI page or one of its assets: the prefix itself or below it, never a sibling path
    that merely starts with the same characters (``/api/v1/assess/ui-other``)."""
    return path == UI_PATH_PREFIX or path.startswith(UI_PATH_PREFIX + "/")


def csp(frame_origin: str | None) -> str:
    """The page CSP; ``None`` (unknown or disabled site) means nobody may frame it."""
    return CSP_BASE.format(ancestors=frame_origin or "'none'")


def page_headers(frame_origin: str | None) -> dict[str, str]:
    return {**PAGE_BASE, "Content-Security-Policy": csp(frame_origin)}


def asset_headers() -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
