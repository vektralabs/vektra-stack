"""An in-process assess API over a real migrated Postgres, as the restricted API role.

``World`` holds one site with its mint key, the token signing keyring, the hand-off key pair and
an ``httpx`` client on a FastAPI app that mounts the production assess router. The key store is a
dict (bearer string -> ``ApiKeyInfo``), the rows behind it are real ``api_keys`` and
``assess_site_keys`` rows, so the site binding is exercised by the route's own SQL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from vektra_assess.api.router import router
from vektra_assess.config import VektraAssessSettings
from vektra_assess.crypto.seal import public_from_private
from vektra_assess.keys import AssessKeyring
from vektra_shared.auth import ApiKeyInfo
from vektra_shared.http_errors import register_error_handlers
from vektra_shared.registry import ProviderRegistry

PLUGIN = "1.0.0"


class Keys:
    def __init__(self, table: dict[str, ApiKeyInfo]) -> None:
        self.table = table

    async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
        return self.table.get(token)


async def make_site(pg: Any) -> uuid.UUID:
    site = uuid.uuid4()
    await pg.run(
        "super",
        "INSERT INTO assess_sites (id, moodle_site_uuid, base_url, release) "
        "VALUES (:i, :u, 'https://m.example', '4.3.1 (Build: 20231009)')",
        {"i": site, "u": str(site)},
    )
    return site


async def make_key(
    pg: Any, scopes: list[str], bind_to: uuid.UUID | None = None, purpose: str = "mint"
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


async def configure_activity(
    pg: Any,
    site: uuid.UUID,
    cmid: int,
    *,
    kind: str = "assign",
    cap_minutes: int | None = 30,
    enabled: bool = True,
    attested: bool = True,
    end_at_sql: str = "NULL",
) -> None:
    """``attested``: a REQ-091 teacher opening date a day ago (the recorded assignments cannot
    list every participant's extension, so without it the gate is NOT_AVAILABLE)."""
    opening = "now() - interval '1 day'" if attested else "NULL"
    await pg.run(
        "super",
        "INSERT INTO assess_activity_config (site_id, cmid, activity_type, enabled, cap_minutes, "
        f"attested_opening_at, end_at) VALUES (:s, :c, :k, :e, :cap, {opening}, {end_at_sql}) "
        "ON CONFLICT (site_id, cmid) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "cap_minutes = EXCLUDED.cap_minutes, attested_opening_at = EXCLUDED.attested_opening_at, "
        "end_at = EXCLUDED.end_at, activity_type = EXCLUDED.activity_type",
        {"s": site, "c": cmid, "k": kind, "e": enabled, "cap": cap_minutes},
    )


async def beat(pg: Any, age: str = "1 second") -> None:
    await pg.run("super", "DELETE FROM assess_heartbeat")
    await pg.run(
        "worker",
        "INSERT INTO assess_heartbeat (component, beat_at, details) VALUES "
        f"('worker', now() - interval '{age}', '{{\"state\": \"ready\"}}'::jsonb)",
    )


def signing_ring(kid: str = "s1") -> AssessKeyring:
    return AssessKeyring("assess_token_signing", kid, {kid: os.urandom(32)})


def handoff_pair(kid: str = "h1") -> tuple[AssessKeyring, AssessKeyring]:
    raw = os.urandom(32)
    return (
        AssessKeyring("assess_handoff_public", kid, {kid: public_from_private(raw)}),
        AssessKeyring("assess_handoff_private", kid, {kid: raw}),
    )


@dataclass
class World:
    pg: Any
    client: httpx.AsyncClient
    app: FastAPI
    engine: AsyncEngine
    site: uuid.UUID
    other_site: uuid.UUID
    keys: dict[str, ApiKeyInfo]
    signing: AssessKeyring
    handoff_public: AssessKeyring
    handoff_private: AssessKeyring
    settings: VektraAssessSettings
    extra: dict[str, Any] = field(default_factory=dict)

    async def mint(
        self,
        sub: int = 101,
        cmid: int = 501,
        key: str = "mint",
        **over: Any,
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "sub": sub,
            "cmid": cmid,
            "courseid": 7,
            "loginas": False,
            "realuser_id": None,
            "plugin_version": PLUGIN,
        }
        body.update(over)
        body = {k: v for k, v in body.items() if v is not _ABSENT}
        return await self.client.post(
            "/api/v1/assess/tokens", json=body, headers=auth(key)
        )

    async def presession(self, sub: int = 101, cmid: int = 501) -> str:
        r = await self.mint(sub, cmid)
        assert r.status_code == 201, r.text
        return str(r.json()["token"])


_ABSENT = object()
ABSENT = _ABSENT


def auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {name}"}


def err(r: httpx.Response) -> dict[str, Any]:
    body = r.json()
    return (body.get("detail") or body)["error"]  # type: ignore[no-any-return]


def default_settings(**over: Any) -> VektraAssessSettings:
    # development: the REQ-089 privacy store arrives with plan 12, BR-006 (h) needs the dev override
    base = {
        "VEKTRA_ASSESS_ENABLED": True,
        "VEKTRA_ENV": "development",
        "VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET": True,
        # the origin of the test client (base_url http://t): the UI page answers there only
        "VEKTRA_ASSESS_UI_ORIGIN": "http://t",
    }
    return VektraAssessSettings.model_validate({**base, **over})


@asynccontextmanager
async def world(
    pg: Any, settings: VektraAssessSettings | None = None, **app_state: Any
) -> AsyncIterator[World]:
    site, other = await make_site(pg), await make_site(pg)
    keys = {
        "mint": await make_key(pg, ["assess:mint"], site, "mint"),
        "mint_other": await make_key(pg, ["assess:mint"], other, "mint"),
        "teacher": await make_key(pg, ["assess:teacher"], site, "teacher"),
        "admin": await make_key(pg, ["admin"]),
        "unbound_mint": await make_key(pg, ["assess:mint"]),
    }
    signing = signing_ring()
    pub, priv = handoff_pair()
    cfg = settings or default_settings()
    app = FastAPI()
    register_error_handlers(app)
    registry = ProviderRegistry()
    registry.register("key_store", "default", Keys(dict(keys)))
    app.state.registry = registry
    engine = create_async_engine(pg.url("api"))
    app.state.assess_engine = engine
    app.state.assess_settings = cfg
    from vektra_admin.rate_limit import RateLimiter
    from vektra_assess.api.state import LIMITER_NAMES

    app.state.assess_limiters = {name: RateLimiter() for name in LIMITER_NAMES}
    app.state.assess_keys = {
        "assess_token_signing": signing,
        "assess_handoff_private": priv,
    }
    for name, value in app_state.items():
        setattr(app.state, name, value)
    app.include_router(router)
    from vektra_assess.ui.router import router as ui_router

    app.include_router(ui_router)
    await beat(pg)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            yield World(
                pg, client, app, engine, site, other, keys, signing, pub, priv, cfg
            )
    finally:
        await engine.dispose()
