"""POST/GET /api/v1/assess/sites/{site}/setup-check on a real database, as the API role."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_assess.api.router import router
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.registry import ProviderRegistry

pytestmark = pytest.mark.integration


class Keys:
    def __init__(self, table: dict[str, ApiKeyInfo]):
        self.table = table

    async def lookup_by_token(self, token: str):
        return self.table.get(token)


async def beat(pg, age="1 second"):
    await pg.run("super", "DELETE FROM assess_heartbeat")
    await pg.run(
        "worker",
        f"INSERT INTO assess_heartbeat (component, beat_at) VALUES ('worker', now() - interval '{age}')",
    )


async def make_site(pg) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:i, :u, 'https://m')",
        {"i": site, "u": str(site)},
    )
    return site


async def make_key(
    pg, scopes: list[str], bind_to=None, purpose="teacher"
) -> ApiKeyInfo:
    kid = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO api_keys (id, key_hash, key_preview, scopes) VALUES (:i, :h, 'prev', :s)",
        {"i": kid, "h": f"h-{kid}", "s": scopes},
    )
    if bind_to is not None:
        await pg.run(
            "super",
            "INSERT INTO assess_site_keys (site_id, api_key_id, purpose) VALUES (:s, :k, :p)",
            {"s": bind_to, "k": kid, "p": purpose},
        )
    return ApiKeyInfo(key_id=kid, scopes=scopes)


@asynccontextmanager
async def world_ctx(pg):
    site_a, site_b = await make_site(pg), await make_site(pg)
    keys = {
        "admin": await make_key(pg, ["admin"]),
        "teacher_a": await make_key(pg, ["assess:teacher"], site_a),
        "teacher_b": await make_key(pg, ["assess:teacher"], site_b),
        "mint_a": await make_key(pg, ["assess:mint"], site_a, "mint"),
        "query": await make_key(pg, ["query"]),
    }
    app = FastAPI()
    registry = ProviderRegistry()
    registry.register(
        "key_store", "default", Keys({name: info for name, info in keys.items()})
    )
    app.state.registry = registry
    engine = create_async_engine(pg.url("api"))
    app.state.assess_engine = engine
    app.include_router(router)
    await beat(pg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        yield client, site_a, site_b
    await engine.dispose()


def err(r):
    body = r.json()
    return (body.get("detail") or body)["error"]


def auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {name}"}


async def test_admin_and_the_sites_own_teacher_key_can_queue_a_check(pg):
    async with world_ctx(pg) as world:
        await _test_admin_and_the_sites_own_teacher_key_can_queue_a_check(world, pg)


async def _test_admin_and_the_sites_own_teacher_key_can_queue_a_check(world, pg):
    client, a, b = world
    r = await client.post(
        f"/api/v1/assess/sites/{a}/setup-check", headers=auth("teacher_a")
    )
    assert r.status_code == 202, r.text
    rid = r.json()["report_id"]
    row = (
        await pg.run(
            "super",
            "SELECT status, requested_by_key_id IS NOT NULL FROM assess_setup_check_reports "
            "WHERE id=:i",
            {"i": rid},
        )
    )[0]
    assert tuple(row) == ("queued", True)
    job = await pg.run(
        "super",
        "SELECT kind, args->>'report_id', args->>'site_id' FROM assess_jobs "
        "WHERE dedupe_key=:k",
        {"k": f"setup_check:{a}"},
    )
    assert [tuple(j) for j in job] == [("setup_check", rid, str(a))]
    # admin on the other site is fine, and is not blocked by site a's rate limit
    r = await client.post(
        f"/api/v1/assess/sites/{b}/setup-check", headers=auth("admin")
    )
    assert r.status_code == 202


async def test_scopes_and_site_binding(pg):
    async with world_ctx(pg) as world:
        await _test_scopes_and_site_binding(world)


async def _test_scopes_and_site_binding(world):
    client, a, _b = world
    url = f"/api/v1/assess/sites/{a}/setup-check"
    assert (await client.post(url)).status_code == 401  # no key
    assert (await client.post(url, headers=auth("nobody"))).status_code == 401
    for name in ("mint_a", "query"):
        r = await client.post(url, headers=auth(name))
        assert r.status_code == 403 and err(r)["code"] == "ERR-AUTH-003", name
    r = await client.post(
        url, headers=auth("teacher_b")
    )  # another site's key: cross-site
    assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-003"
    r = await client.post(
        f"/api/v1/assess/sites/{uuid.uuid4()}/setup-check", headers=auth("admin")
    )
    assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-003"


async def test_one_check_per_site_per_five_minutes(pg):
    async with world_ctx(pg) as world:
        await _test_one_check_per_site_per_five_minutes(world, pg)


async def _test_one_check_per_site_per_five_minutes(world, pg):
    client, a, _ = world
    url = f"/api/v1/assess/sites/{a}/setup-check"
    assert (await client.post(url, headers=auth("teacher_a"))).status_code == 202
    r = await client.post(url, headers=auth("teacher_a"))
    assert r.status_code == 429 and err(r)["code"] == "ERR-ASSESS-002"
    assert 1 <= int(r.headers["retry-after"]) <= 300
    assert (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_setup_check_reports WHERE site_id=:s",
            {"s": a},
        )
    )[0][0] == 1
    await pg.run(
        "super",
        "UPDATE assess_setup_check_reports SET created_at = now() - interval '6 minutes' "
        "WHERE site_id=:s",
        {"s": a},
    )
    assert (await client.post(url, headers=auth("teacher_a"))).status_code == 202


async def test_no_worker_heartbeat_is_503_not_a_forever_queued_report(pg):
    async with world_ctx(pg) as world:
        await _test_no_worker_heartbeat_is_503_not_a_forever_queued_report(world, pg)


async def _test_no_worker_heartbeat_is_503_not_a_forever_queued_report(world, pg):
    client, a, _ = world
    url = f"/api/v1/assess/sites/{a}/setup-check"
    await pg.run("super", "DELETE FROM assess_heartbeat")
    r = await client.post(url, headers=auth("teacher_a"))
    assert r.status_code == 503 and err(r)["code"] == "ERR-ASSESS-001"
    assert "worker_heartbeat" in err(r)["remediation"]
    await beat(pg, age="5 minutes")  # stale heartbeat is the same as none
    assert (await client.post(url, headers=auth("teacher_a"))).status_code == 503
    assert (
        await pg.run(
            "super",
            "SELECT count(*) FROM assess_setup_check_reports WHERE site_id=:s",
            {"s": a},
        )
    )[0][0] == 0


async def test_get_report_is_content_free_and_site_scoped(pg):
    async with world_ctx(pg) as world:
        await _test_get_report_is_content_free_and_site_scoped(world, pg)


async def _test_get_report_is_content_free_and_site_scoped(world, pg):
    client, a, b = world
    rid = (
        await client.post(
            f"/api/v1/assess/sites/{a}/setup-check", headers=auth("teacher_a")
        )
    ).json()["report_id"]
    await pg.run(
        "worker",
        "UPDATE assess_setup_check_reports SET status='done', moodle_release='5.2.4+', "
        "items = CAST(:i AS jsonb), finished_at=now() WHERE id=:r",
        {
            "r": rid,
            "i": '[{"name":"token_works","result":"OK","reason_code":null,"leak":"Mario Rossi"}]',
        },
    )
    r = await client.get(
        f"/api/v1/assess/sites/{a}/setup-check/{rid}", headers=auth("teacher_a")
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done" and body["moodle_release"] == "5.2.4+"
    assert body["items"] == [
        {"name": "token_works", "result": "OK", "reason_code": None}
    ]
    assert "Mario" not in r.text  # only name/result/reason_code are ever returned
    # the same report id through another site's route, or another site's key: 404
    assert (
        await client.get(
            f"/api/v1/assess/sites/{b}/setup-check/{rid}", headers=auth("admin")
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/assess/sites/{a}/setup-check/{rid}", headers=auth("teacher_b")
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/assess/sites/{a}/setup-check/{uuid.uuid4()}",
            headers=auth("admin"),
        )
    ).status_code == 404
