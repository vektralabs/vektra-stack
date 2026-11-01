"""Every /api/v1/assess route is guarded by an exact scope or the assess token verifier.

Plan assess-foundations 4.5 (ARCH-067, NFR-014). ``require_scope`` honours the admin
superscope (ARCH-059), so an assess route guarded by it would let any Vektra admin key
mint discussion tokens. The guard is structural: it walks each route's dependency tree.

SKIPPED, with an explicit reason, while no assess route is registered; plan
assess-session-lifecycle adds the routes, and with them this test starts to bite.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from vektra_shared.auth import require_exact_scope, require_scope

ASSESS_PREFIX = "/api/v1/assess"
ALLOWED_PREFIXES = ("require_exact_scope_", "verify_assess_token")
FORBIDDEN_PREFIX = "require_scope_"


def _dependency_names(dependant) -> Iterator[str]:  # type: ignore[no-untyped-def]
    for dep in dependant.dependencies:
        call = dep.call
        if call is not None:
            yield getattr(call, "__name__", repr(call))
        yield from _dependency_names(dep)


def _violations(app: FastAPI) -> tuple[int, list[str]]:
    count, bad = 0, []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(ASSESS_PREFIX):
            continue
        count += 1
        names = list(_dependency_names(route.dependant))
        if any(n.startswith(FORBIDDEN_PREFIX) for n in names):
            bad.append(f"{route.path}: uses require_scope (admin superscope)")
        elif not any(n.startswith(ALLOWED_PREFIXES) for n in names):
            bad.append(f"{route.path}: no exact scope or assess token dependency")
    return count, bad


def test_inspector_is_not_vacuous() -> None:
    app = FastAPI()

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
    assert count == 3
    assert len(bad_routes) == 2
    assert any("/bad" in b for b in bad_routes) and any(
        "/open" in b for b in bad_routes
    )


def test_assess_routes_use_exact_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from vektra_app.main import create_app

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    count, bad = _violations(create_app())
    if count == 0:
        pytest.skip("no assess routes registered yet (plan assess-session-lifecycle)")
    assert bad == []
