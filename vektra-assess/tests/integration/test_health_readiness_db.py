"""/health/assess readiness over the API role on real Postgres (plan 12b, COMP-002).

The verdict comes from worker-written rows only; the details read nothing the API role may not
read, and carry no identifier.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vektra_admin.health import check_component_http
from vektra_assess.health import make_readiness_check
from vektra_shared.registry import ProviderRegistry

from .conftest import make_session

pytestmark = pytest.mark.integration


async def _beat(pg, component: str, details: dict, age_s: int = 0) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "INSERT INTO assess_heartbeat (component, beat_at, details) VALUES "
        "(:c, now() - make_interval(secs => :a), CAST(:d AS jsonb)) ON CONFLICT (component) "
        "DO UPDATE SET beat_at = EXCLUDED.beat_at, details = EXCLUDED.details",
        {"c": component, "a": age_s, "d": json.dumps(details)},
    )


async def test_readiness_follows_the_worker_rows_and_answers_503(pg) -> None:  # type: ignore[no-untyped-def]
    sid = await make_session(pg, "ACTIVE")
    site = (
        await pg.run(
            "super",
            "SELECT site_id::text FROM assess_sessions WHERE id = :s",
            {"s": sid},
        )
    )[0][0]
    engine = create_async_engine(pg.url("api"))
    registry = ProviderRegistry()
    registry.register(
        "health",
        "assess",
        make_readiness_check(
            enabled=True, get_engine=lambda: engine, keys_present=lambda: True
        ),
    )
    try:
        await pg.run("super", "DELETE FROM assess_heartbeat")
        body, code = await check_component_http(registry, "assess")
        assert (code, body.message) == (503, "no_heartbeat")

        await _beat(pg, "worker", {"state": "ready", "oldest_ready_age_s": 0.4,
                                   "parser": {"ok": True, "at": 0}})  # fmt: skip
        await _beat(pg, "smtp", {"state": "ok", "ok": True}, age_s=20)
        await _beat(pg, "purge", {"ok": True, "last_success": 0, "rows_deleted": 3})
        body, code = await check_component_http(registry, "assess")
        assert (code, body.status) == (200, "healthy"), body
        d = body.details
        assert d is not None and d["worker_state"] == "ready"
        assert d["smtp_probe"]["status"] == "ok" and d["smtp_probe"]["age_s"] >= 19
        assert (
            d["privacy"]["active_sites"] >= 1 and d["privacy"]["configured_sites"] == 0
        )
        assert d["parser"]["status"] == "stale"  # pinged at epoch 0
        assert site not in json.dumps(d) and str(sid) not in json.dumps(d)

        # review #7: the configured count is computed in SQL; a URL without a host is not one
        for url in ("https://uni.example/privacy", "https:///no-host"):
            other_sid = await make_session(pg, "ACTIVE")
            other = (
                await pg.run(
                    "super",
                    "SELECT site_id FROM assess_sessions WHERE id = :s",
                    {"s": other_sid},
                )
            )[0][0]
            await pg.run(
                "super",
                "INSERT INTO assess_privacy_config (site_id, retention_days, "
                "legal_basis_label, legal_basis_text, notice_url, notice_text, "
                "notice_version, updated_by) VALUES (:s, 30, 'l', 't', :u, 'n', '1', 'op')",
                {"s": other, "u": url},
            )
        body, _ = await check_component_http(registry, "assess")
        assert body.details is not None
        assert body.details["privacy"]["configured_sites"] == 1

        await _beat(
            pg, "worker", {"state": "ready"}, age_s=120
        )  # a kill -9: the row ages
        body, code = await check_component_http(registry, "assess")
        assert (code, body.message) == (503, "heartbeat_stale")
        await _beat(pg, "worker", {"state": "stopped"})  # a SIGTERM
        body, code = await check_component_http(registry, "assess")
        assert (code, body.message) == (503, "worker_not_ready")
    finally:
        await engine.dispose()
