"""``GET /api/v1/assess/ui`` and its assets (ARCH-076, INT-001; plan 9).

The page is public by design: it carries no data and no credential, only the site's Moodle
origin (the one origin allowed to frame it and to talk to it by ``postMessage``) in a JSON data
block that is never executed. ``site`` is the site's public id (``assess_sites.id``), never a
credential; an unknown, malformed or disabled site is 404 and may be framed by nobody. Tokens
reach the page only by ``postMessage`` from the parent, never through the URL.

Assets are served from the built bundle's ``manifest.json`` only (content-hashed names), never
through a generic static mount: a name that is not in the manifest is 404, so no path
traversal can reach another file. Without a built bundle the page is 503.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text

from vektra_assess.api.common import engine_of
from vektra_assess.ui.headers import asset_headers, page_headers

PREFIX = "/api/v1/assess/ui"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html", "j2"], default=True),
)

_SITE = text(
    "SELECT id, base_url, moodle_origin, disabled_at FROM assess_sites "
    "WHERE id = CAST(:s AS uuid)"
)


def site_origin_sql() -> str:
    """The Moodle origins of the enabled sites, one per row (the origins that may frame the
    page): ``moodle_origin`` (revision 0033), or the origin of ``base_url`` when unset."""
    return (
        "SELECT coalesce(moodle_origin, lower(substring(base_url from "
        "'^(https?://[^/?#@]+)'))) FROM assess_sites WHERE disabled_at IS NULL"
    )


def public_assess_ui() -> None:
    """Marker dependency: this route is unauthenticated by design (ARCH-076). The route-scope
    structural test accepts it on the UI paths only."""


router = APIRouter(prefix=PREFIX, dependencies=[Depends(public_assess_ui)])


def static_dir(request: Request) -> Path:
    override = getattr(request.app.state, "assess_ui_static_dir", None)
    return Path(override) if override else STATIC_DIR


def load_manifest(directory: Path) -> dict[str, str] | None:
    """``{"js": name, "css": name}`` of the built bundle, or None when it is not built."""
    try:
        data = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not {"js", "css"} <= set(data):
        return None
    return {k: str(data[k]) for k in ("js", "css")}


def origin_of(url: str) -> str | None:
    """scheme://host[:port] of an absolute http(s) URL, or None."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _json_block(data: dict[str, Any]) -> str:
    # a JSON data block inside <script>: no '<', '>' or '&' may appear raw
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


async def _site_origin(request: Request, site: str) -> str | None:
    try:
        UUID(site)
    except ValueError:
        return None
    async with engine_of(request).connect() as conn:
        row = (await conn.execute(_SITE, {"s": site})).first()
    if row is None or row.disabled_at is not None:
        return None
    # the site's own Moodle origin (revision 0033); a row registered without one falls back to
    # the origin of its base_url
    return str(row.moodle_origin) if row.moodle_origin else origin_of(str(row.base_url))


@router.get("", response_class=HTMLResponse)
async def ui_page(request: Request, site: str = "") -> Response:
    origin = await _site_origin(request, site)
    if origin is None:
        return Response(status_code=404, headers=page_headers(None))
    manifest = load_manifest(static_dir(request))
    if manifest is None:
        return Response(status_code=503, headers=page_headers(None))
    html = TEMPLATES.get_template("page.html.j2").render(
        cfg_json=_json_block(
            {"site": site, "moodleOrigin": origin, "apiBase": "/api/v1/assess"}
        ),
        js_url=f"{PREFIX}/assets/{manifest['js']}",
        css_url=f"{PREFIX}/assets/{manifest['css']}",
    )
    # the JSON block is escaped for a <script> data block, not for HTML text: mark it safe
    # only there (the template autoescapes everything else)
    return HTMLResponse(html, headers=page_headers(origin))


@router.get("/assets/{name}")
async def ui_asset(request: Request, name: str) -> Response:
    directory = static_dir(request)
    manifest = load_manifest(directory)
    if manifest is None or name not in manifest.values():
        return Response(status_code=404, headers=asset_headers())
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return FileResponse(directory / name, media_type=media, headers=asset_headers())
