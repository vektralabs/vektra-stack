"""Response headers of the discussion UI route and its assets (ARCH-076, plan 9).

Plan 9 task 1 ships the cache and sniffing headers; the strict CSP with the site's
``frame-ancestors`` is the header builder task.
"""

from __future__ import annotations

PAGE_BASE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def page_headers(frame_origin: str | None) -> dict[str, str]:
    """Headers of the page; ``frame_origin`` None means the page may be framed by nobody."""
    return dict(PAGE_BASE)


def asset_headers() -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
