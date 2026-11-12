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
    assert event.contains(engine.sync_engine, "commit", sb._on_end)
    assert event.contains(engine.sync_engine, "rollback", sb._on_end)

    emitted: list[tuple[str, dict[str, str]]] = []

    class Conn:
        def __init__(self) -> None:
            self.info: dict[str, Any] = {"vektra_assess_tx_site": "stale"}

        def execute(self, stmt: Any, params: dict[str, str]) -> None:
            emitted.append((str(stmt), params))

    a, b = uuid.uuid4(), uuid.uuid4()

    def run() -> None:
        unbound = Conn()
        sb._on_begin(unbound)  # unbound: nothing emitted, the policy then reads nothing
        assert unbound.info == {}  # and no marker left from an earlier transaction
        for site in (a, b):
            sb.reset_site()
            sb.set_request_site(site)
            conn = Conn()
            sb._on_begin(conn)
            assert conn.info == {"vektra_assess_tx_site": site}

    _fresh.run(run)
    # review finding 3: the site is a bound parameter; the text is one constant
    assert [p for _s, p in emitted] == [{"site": str(a)}, {"site": str(b)}]
    assert {s for s, _p in emitted} == {
        "SELECT pg_catalog.set_config('app.assess_site_id', :site, true)"
    }
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


def test_a_site_the_binding_refuses_is_a_token_failure_never_a_500(
    _fresh: Any,
) -> None:
    """Review finding 4: in the student verifiers, a conflicting site turns into the
    ``TokenError`` their callers audit and answer with the 401 envelope."""
    from vektra_assess.api import gate
    from vektra_assess.tokens import PreClaims, TokenError

    claims = PreClaims(1, uuid.uuid4(), 5, "jti-0123456789", 0, 60)

    def run() -> None:
        sb.set_request_site(uuid.uuid4())  # another site already bound to the request
        with pytest.raises(TokenError):
            gate._bound(claims)

    _fresh.run(run)


def test_a_download_token_whose_site_is_refused_is_the_download_401(
    _fresh: Any,
) -> None:
    from fastapi import FastAPI, HTTPException
    from starlette.requests import Request

    from vektra_assess.api import transcripts
    from vektra_assess.api.download_tokens import DownloadClaims

    claims = DownloadClaims(1, uuid.uuid4(), uuid.uuid4(), "owner", "jti")

    class Tokens:
        def verify(self, token: str) -> DownloadClaims:
            return claims

    app = FastAPI()
    app.state.assess_download_tokens = Tokens()
    request = Request(
        {"type": "http", "app": app, "headers": [(b"authorization", b"Bearer t")]}
    )

    def run() -> None:
        sb.set_request_site(uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            asyncio.run(transcripts.verify_assess_token_download(request))
        assert exc.value.status_code == 401

    _fresh.run(run)
