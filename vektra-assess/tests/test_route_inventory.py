"""Plan 9 (ADR-0030, REQ-102): the assess route inventory, statically.

No assess route (API or discussion UI) declares a query or cookie parameter that could carry a
credential, and no route regenerates, edits or deletes a turn or takes an upload. The dynamic
half (a valid token in ``?token=`` or a cookie alone is 401 on every route) runs against a real
database in ``tests/integration/test_route_credentials.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from vektra_assess.api.router import router
from vektra_assess.ui.router import router as ui_router

CREDENTIAL = re.compile(
    r"^(token|access_token|jwt|bearer|auth|authorization|key)$", re.I
)


def routes() -> Iterator[tuple[str, set[str], Any]]:
    app = FastAPI()
    app.include_router(router)
    app.include_router(ui_router)
    for route in app.routes:
        entries = [route] if isinstance(route, APIRoute) else []
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            entries += list(contexts())
        for entry in entries:
            yield (
                entry.path,
                set(getattr(entry, "methods", None) or ()),
                entry.dependant,
            )


def _all_params(dependant: Any, kind: str) -> Iterator[str]:
    for param in getattr(dependant, f"{kind}_params", []):
        yield param.alias or param.name
    for sub in dependant.dependencies:
        yield from _all_params(sub, kind)


def test_the_inventory_covers_the_api_and_the_ui() -> None:
    paths = {p for p, _m, _d in routes()}
    assert "/api/v1/assess/sessions/{sid}/events" in paths
    assert "/api/v1/assess/ui" in paths and "/api/v1/assess/ui/assets/{name}" in paths


def test_no_route_reads_a_credential_from_the_query_or_a_cookie() -> None:
    for path, _methods, dependant in routes():
        for kind in ("query", "cookie"):
            for name in _all_params(dependant, kind):
                assert not CREDENTIAL.match(name), (path, kind, name)


def test_no_route_regenerates_edits_deletes_or_uploads() -> None:
    forbidden = re.compile(r"regenerat|edit|upload|attach|rewrite|undo", re.I)
    for path, methods, dependant in routes():
        assert not forbidden.search(path), path
        assert not ({"PUT", "PATCH", "DELETE"} & methods and "/turns" in path), path
        for param in getattr(dependant, "body_params", []):
            assert "UploadFile" not in repr(param), path


def test_the_query_check_bites() -> None:
    probe = FastAPI()

    @probe.get("/api/v1/assess/probe")
    async def leaky(token: str = "") -> None:
        return None

    route = next(
        r for r in probe.routes if isinstance(r, APIRoute) and r.path.endswith("probe")
    )
    assert "token" in set(_all_params(route.dependant, "query"))


# --- plan assess-teacher-ops 11b: the teacher routes ----------------------------------------

TEACHER_PREFIX = "/api/v1/assess/activities/"
TEACHER_ROUTES = {
    ("/api/v1/assess/activities/{cmid}/config", "GET"),
    ("/api/v1/assess/activities/{cmid}/config", "PUT"),
    ("/api/v1/assess/activities/{cmid}/sessions", "GET"),
    ("/api/v1/assess/activities/{cmid}/sessions/{sid}/reset", "POST"),
    ("/api/v1/assess/activities/{cmid}/terminate", "POST"),
    ("/api/v1/assess/activities/{cmid}/students/{userid}/extra-time", "PUT"),
    ("/api/v1/assess/activities/{cmid}/opening-fallback", "PUT"),
    # the plugin asks for a fresh LMS read when a teacher page opens (live E2E follow-up)
    ("/api/v1/assess/activities/{cmid}/gate-refresh", "POST"),
}


def _dependency_names(dependant: Any) -> Iterator[str]:
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield getattr(dep.call, "__name__", repr(dep.call))
        yield from _dependency_names(dep)


def test_no_preview_route_exists() -> None:
    """REQ-090 (could) is not built in v1: no route may offer it, not even dormant."""
    for path, _methods, _dependant in routes():
        assert "preview" not in path.lower(), path


def test_every_teacher_route_requires_exactly_the_teacher_scope() -> None:
    """Every route under ``/activities/`` (and only the declared set) depends on
    ``require_exact_scope("assess:teacher")``: an admin key or a mint key never reaches one."""
    found = set()
    for path, methods, dependant in routes():
        if not path.startswith(TEACHER_PREFIX):
            continue
        names = list(_dependency_names(dependant))
        assert "require_exact_scope_assess:teacher" in names, path
        assert not [n for n in names if n.startswith("require_scope_")], path
        assert "verify_assess_token" not in " ".join(names), path
        found |= {(path, m) for m in methods}
    assert found == TEACHER_ROUTES
