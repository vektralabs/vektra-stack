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

import functools
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text

from vektra_assess.api.common import engine_of
from vektra_assess.api.deps import settings_of
from vektra_assess.ui.headers import asset_headers, page_headers
from vektra_assess.ui.origins import site_origin, url_origin

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


def public_assess_ui() -> None:
    """Marker dependency: this route is unauthenticated by design (ARCH-076). The route-scope
    structural test accepts it on the UI paths only."""


router = APIRouter(prefix=PREFIX, dependencies=[Depends(public_assess_ui)])


def static_dir(request: Request) -> Path:
    override = getattr(request.app.state, "assess_ui_static_dir", None)
    return Path(override) if override else STATIC_DIR


@functools.cache
def manifest_of(directory: Path) -> dict[str, str] | None:
    """``load_manifest`` once per directory for the process lifetime (review #9): the bundle
    is part of the image, a new bundle is a new process. The directory is ``STATIC_DIR`` in
    production; tests point ``app.state.assess_ui_static_dir`` at their own fixture bundle."""
    return load_manifest(directory)


def load_manifest(directory: Path) -> dict[str, str] | None:
    """``{"js": name, "css": name}`` of the built bundle, or None when it is not built."""
    try:
        data = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not {"js", "css"} <= set(data):
        return None
    return {k: str(data[k]) for k in ("js", "css")}


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
    # the origin of its base_url; both through the one normalisation (ui.origins)
    return site_origin(row.moodle_origin, row.base_url)


def request_origin(request: Request) -> str | None:
    """The origin this request was addressed to: the scheme the server saw (uvicorn applies
    ``X-Forwarded-Proto`` only for a proxy in ``FORWARDED_ALLOW_IPS``, its ProxyHeadersMiddleware)
    and the ``Host`` header (the nginx example forwards the client's own), normalised."""
    host = request.headers.get("host")
    return url_origin(f"{request.url.scheme}://{host}") if host else None


def on_ui_origin(request: Request) -> bool:
    """Review #1: the page answers on ``VEKTRA_ASSESS_UI_ORIGIN`` only. Served on another host
    (the API's own origin, a Moodle origin) the plugin's ``allow-same-origin`` sandbox would
    no longer separate it from the parent, so it is not served at all."""
    ui = settings_of(request).ui_origin
    return bool(ui) and request_origin(request) == ui


@router.get("", response_class=HTMLResponse)
async def ui_page(request: Request, site: str = "") -> Response:
    if not on_ui_origin(request):
        return Response(status_code=503, headers=page_headers(None))
    origin = await _site_origin(request, site)
    if origin is None:
        return Response(status_code=404, headers=page_headers(None))
    manifest = manifest_of(static_dir(request))
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
    manifest = manifest_of(directory)
    if manifest is None or name not in manifest.values():
        return Response(status_code=404, headers=asset_headers())
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return FileResponse(directory / name, media_type=media, headers=asset_headers())
