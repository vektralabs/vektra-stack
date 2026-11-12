"""The request's site binding, without a database (ADR-0033 D9.2; the database side, RLS
itself, is ``integration/test_api_rls.py``)."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.api import site_binding as sb
from vektra_assess.api.router import router


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    """Each test in its own context: a binding never leaks between tests."""
    ctx = contextvars.copy_context()
    ctx.run(sb.reset_site)
    yield ctx


def test_a_request_starts_unbound_and_binds_one_site(_fresh: Any) -> None:
    site = uuid.uuid4()

    def run() -> None:
        assert sb.bound_site() is None
        assert (
            sb.set_request_site(str(site)) == site
        )  # a string is parsed and canonical
        assert sb.set_request_site(site) == site  # the same site again is fine
        with pytest.raises(sb.SiteBindingConflict, match="already bound"):
            sb.set_request_site(uuid.uuid4())
        assert sb.bound_site() == site  # the refusal left the binding as it was
        sb.reset_site()
        assert sb.bound_site() is None

    _fresh.run(run)


def test_a_malformed_site_is_refused_before_any_sql(_fresh: Any) -> None:
    def run() -> None:
        with pytest.raises(ValueError):
            sb.set_request_site("x'; SELECT 1; --")
        assert sb.bound_site() is None

    _fresh.run(run)


def test_the_begin_listener_is_installed_once_and_emits_the_canonical_site(
    _fresh: Any,
) -> None:
    engine = create_async_engine("postgresql+asyncpg://u@localhost/db")
    sb.install_site_binding(engine)
    sb.install_site_binding(engine)
    assert event.contains(engine.sync_engine, "begin", sb._on_begin)

    emitted: list[str] = []

    class Conn:
        def exec_driver_sql(self, sql: str) -> None:
            emitted.append(sql)

    site = uuid.uuid4()

    def run() -> None:
        sb._on_begin(Conn())  # unbound: nothing emitted, the policy then reads nothing
        sb.set_request_site(site)
        sb._on_begin(Conn())

    _fresh.run(run)
    assert emitted == [
        f"SELECT pg_catalog.set_config('app.assess_site_id', '{site}', true)"
    ]
    asyncio.run(engine.dispose())


def test_a_task_started_by_the_request_inherits_its_site(_fresh: Any) -> None:
    """The reply task of a turn is started inside the request: it reads the same site."""
    site = uuid.uuid4()

    async def main() -> uuid.UUID | None:
        sb.set_request_site(site)

        async def child() -> uuid.UUID | None:
            return sb.bound_site()

        return await asyncio.create_task(child())

    assert _fresh.run(asyncio.run, main()) == site


def test_the_router_resets_the_binding_before_anything_else() -> None:
    """Every assess route's first dependency is the reset: no request inherits a site."""
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    first = router.dependencies[0].dependency
    assert first is sb.reset_site_dependency
    app = FastAPI()
    app.include_router(router)
    # FastAPI 0.139 keeps an included router as one entry: walk its effective contexts too
    # (as vektra-app/tests/test_assess_route_scopes.py does)
    found: list[tuple[str, Any]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            found.append((route.path, route.dependant))
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            found.extend((ctx.path, ctx.dependant) for ctx in contexts())
    assert len(found) > 25, "the router's routes were not found"
    for path, dependant in found:
        deps = [d.call for d in dependant.dependencies]
        assert deps[0] is sb.reset_site_dependency, path


def test_the_token_verifiers_bind_the_site_the_token_names() -> None:
    """Each student/download token dependency binds the signed site (static: the call is in
    the function, after a successful verification)."""
    import inspect

    from vektra_assess.api import gate, transcripts

    for fn in (
        gate.verify_assess_token_pre,
        gate.verify_assess_token_session,
        gate.verify_assess_token_any,
    ):
        assert "_bound(" in inspect.getsource(fn), fn.__name__
    assert "set_request_site(claims.site)" in inspect.getsource(gate._bound)
    assert "set_request_site(claims.site)" in inspect.getsource(
        transcripts.verify_assess_token_download
    )
