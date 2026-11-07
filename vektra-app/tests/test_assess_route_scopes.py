"""Every /api/v1/assess route is guarded by an exact scope or the assess token verifier.

Plan assess-foundations 4.5 (ARCH-067, NFR-014). ``require_scope`` honours the admin
superscope (ARCH-059), so an assess route guarded by it would let any Vektra admin key
mint discussion tokens. The guard is structural: it walks each route's dependency tree.

SKIPPED, with an explicit reason, while no assess route is registered; plan
assess-session-lifecycle adds the routes, and with them this test starts to bite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from vektra_shared.auth import require_exact_scope, require_scope

ASSESS_PREFIX = "/api/v1/assess"
ALLOWED_PREFIXES = ("require_exact_scope_", "verify_assess_token")
#: the only unauthenticated assess paths (plan 9): the UI page carries no data and no credential
UI_PREFIX = "/api/v1/assess/ui"
FORBIDDEN_PREFIX = "require_scope_"


def _dependency_names(dependant) -> Iterator[str]:  # type: ignore[no-untyped-def]
    for dep in dependant.dependencies:
        call = dep.call
        if call is not None:
            yield getattr(call, "__name__", repr(call))
        yield from _dependency_names(dep)


def iter_routes(app: FastAPI) -> Iterator[tuple[str, Any]]:
    """(path, dependant) of every HTTP route, including routes of included routers.

    FastAPI 0.139 keeps an included router as one ``_IncludedRouter`` entry in
    ``app.routes`` instead of flattening its APIRoutes, so filtering ``app.routes`` on
    ``APIRoute`` silently sees none of them. Walk the effective contexts too.
    """
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route.path, route.dependant
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            for ctx in contexts():
                yield ctx.path, ctx.dependant


def _violations(app: FastAPI) -> tuple[int, list[str]]:
    count, bad = 0, []
    for path, dependant in iter_routes(app):
        if not path.startswith(ASSESS_PREFIX):
            continue
        count += 1
        names = list(_dependency_names(dependant))
        if any(n.startswith(FORBIDDEN_PREFIX) for n in names):
            bad.append(f"{path}: uses require_scope (admin superscope)")
        elif path.startswith(UI_PREFIX) and "public_assess_ui" in names:
            continue  # the discussion UI page and assets: public by design (ARCH-076)
        elif not any(n.startswith(ALLOWED_PREFIXES) for n in names):
            bad.append(f"{path}: no exact scope or assess token dependency")
    return count, bad


def test_inspector_is_not_vacuous() -> None:
    """Routes declared on the app AND on an included router are both inspected."""
    app = FastAPI()
    included = APIRouter(prefix=ASSESS_PREFIX)

    @included.get("/included-bad")
    async def included_bad(_=Depends(require_scope("query"))):  # type: ignore[no-untyped-def]
        return {}

    app.include_router(included)

    @app.get(f"{ASSESS_PREFIX}/bad")
    async def bad(_=Depends(require_scope("admin"))):  # type: ignore[no-untyped-def]
        return {}

    @app.get(f"{ASSESS_PREFIX}/open")
    async def open_route():  # type: ignore[no-untyped-def]
        return {}

    @app.get(f"{ASSESS_PREFIX}/good")
    async def good(_=Depends(require_exact_scope("assess:mint"))):  # type: ignore[no-untyped-def]
        return {}

    count, bad_routes = _violations(app)
    assert count == 4
    assert len(bad_routes) == 3
    for frag in ("/bad", "/open", "/included-bad"):
        assert any(frag in b for b in bad_routes), frag


def test_assess_routes_use_exact_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from vektra_app.main import create_app

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    count, bad = _violations(create_app())
    if count == 0:
        pytest.skip("no assess routes registered yet (plan assess-session-lifecycle)")
    assert bad == []
