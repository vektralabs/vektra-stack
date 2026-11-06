"""M2.7 / ADR-0030 (dynamic half): every mounted assess route refuses a VALID credential passed
in the query string (``?token=``, ``?access_token=``, ``?key=``) or in a cookie, without the
Authorization header: 401, and nothing is written.

The inventory walks the production router, so a route added later is covered without editing
this file; the test fails if the walk finds fewer routes than this plan mounts (non-vacuous).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import create_async_engine
from support.sessions import seed_session
from support.world import world

from integration.test_session_preflight import _reset
from vektra_assess.api.router import router

pytestmark = pytest.mark.integration

#: routes this plan mounts (PR 8a); the walk must find at least these
EXPECTED = {
    ("POST", "/api/v1/assess/tokens"),
    ("POST", "/api/v1/assess/sessions"),
    ("GET", "/api/v1/assess/sessions/{sid}"),
    ("GET", "/api/v1/assess/sessions/{sid}/events"),
    ("POST", "/api/v1/assess/sessions/{sid}/turns"),
    ("GET", "/api/v1/assess/state"),
    ("POST", "/api/v1/assess/sessions/{sid}/finish"),
}


def inventory() -> Iterator[tuple[str, str]]:
    """(method, path) of every route the production router mounts, included routers too
    (FastAPI keeps an included router as one entry: walk its effective contexts)."""
    app = FastAPI()
    app.include_router(router)
    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        entries = [route] if isinstance(route, APIRoute) else []
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            entries += list(contexts())
        for entry in entries:
            for method in sorted(getattr(entry, "methods", None) or ()):
                if (method, entry.path) not in seen and entry.path.startswith(
                    "/api/v1/assess"
                ):
                    seen.add((method, entry.path))
                    yield method, entry.path


def test_the_inventory_is_not_vacuous() -> None:
    found = set(inventory())
    assert EXPECTED <= found, sorted(EXPECTED - found)


async def test_no_route_accepts_a_query_string_or_cookie_credential(pg) -> None:  # type: ignore[no-untyped-def]
    await _reset(pg)
    worker = create_async_engine(pg.url("worker"))
    try:
        async with world(pg) as w:
            s = await seed_session(w, worker, "ACTIVE")
            pre = await w.presession(sub=s.sub, cmid=s.cmid)
            valid = {
                "session": s.session_token,
                "pre": pre,
                "key": "admin",  # a valid API-key bearer of the world's key store
                "mint": "mint",
            }
            ids = {
                "sid": str(s.session_id),
                "site_id": str(w.site),
                "report_id": str(uuid.uuid4()),
            }
            before = (
                await pg.run(
                    "super",
                    "SELECT (SELECT count(*) FROM assess_sessions) + "
                    "(SELECT count(*) FROM assess_turns) + "
                    "(SELECT count(*) FROM assess_token_jti) + "
                    "(SELECT count(*) FROM assess_jobs)",
                )
            )[0][0]
            checked = 0
            for method, path in inventory():
                url = path.format(**ids)
                for cred in valid.values():
                    for query in (
                        f"token={cred}",
                        f"access_token={cred}",
                        f"key={cred}",
                    ):
                        r = await w.client.request(
                            method,
                            f"{url}?{query}",
                            json={} if method in ("POST", "PUT") else None,
                            headers={"Cookie": f"token={cred}; access_token={cred}"},
                        )
                        assert r.status_code == 401, (
                            method,
                            path,
                            query,
                            r.status_code,
                        )
                        checked += 1
            assert checked >= len(EXPECTED) * 12
            after = (
                await pg.run(
                    "super",
                    "SELECT (SELECT count(*) FROM assess_sessions) + "
                    "(SELECT count(*) FROM assess_turns) + "
                    "(SELECT count(*) FROM assess_token_jti) + "
                    "(SELECT count(*) FROM assess_jobs)",
                )
            )[0][0]
            assert after == before  # nothing was minted, started, appended or queued
    finally:
        await worker.dispose()
