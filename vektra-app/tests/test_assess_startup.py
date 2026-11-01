"""Assess wiring in the API process (plan assess-foundations 6.1, 6.1b, 6.5).

The route table is the evidence for dormancy, not a status field; the key-matrix guard
is driven into refusal with real files; the role check asks a real Postgres who the
session is (integration).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from vektra_app import assess_wiring
from vektra_shared.registry import ProviderRegistry
from vektra_shared.startup import StartupValidationError

MAIN_DSN = "postgresql+asyncpg://vektra:pw@db:5432/vektra"


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


def _assess_paths(app: FastAPI) -> list[str]:
    return [p for p, _ in iter_routes(app) if p.startswith("/api/v1/assess")]


def _state_app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


@pytest.fixture
def secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    return d


@pytest.fixture
def probe_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the (still empty) assess router one route, so mounting is observable."""
    from vektra_assess.api import router as router_mod

    r = APIRouter(prefix=router_mod.PREFIX)

    @r.get("/probe")
    async def probe() -> dict[str, str]:
        return {"ok": "yes"}

    monkeypatch.setattr(router_mod, "router", r)


# --- 6.1b: dormant by default -------------------------------------------------


def test_disabled_by_default_mounts_no_assess_route(probe_router: None) -> None:
    from vektra_app.main import create_app

    app = create_app()
    assert _assess_paths(app) == []
    app.state.registry = ProviderRegistry()
    resp = TestClient(app).get("/api/v1/assess/probe")
    assert resp.status_code == 404


def test_enabled_mounts_the_router(
    probe_router: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vektra_app.main import create_app

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    assert _assess_paths(create_app()) == ["/api/v1/assess/probe"]


def test_broken_assess_config_mounts_nothing(
    probe_router: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vektra_app.main import create_app

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "not-a-bool")
    assert _assess_paths(create_app()) == []


# --- 6.5: step 12 refusals ------------------------------------------------------


def _write_keyring(d: Path, name: str) -> None:
    p = d / name
    p.write_text("{}")
    os.chmod(p, 0o400)


async def test_disabled_api_with_ws_keyring_refuses(secrets_dir: Path) -> None:
    _write_keyring(secrets_dir, "assess_ws_tokens")
    with pytest.raises(StartupValidationError) as exc:
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]
    assert exc.value.step == "assess_check"
    assert "assess_ws_tokens: forbidden for api" in exc.value.detail
    assert "[STARTUP ERROR] Step: assess_check" in exc.value.to_plain_text()


async def test_disabled_api_with_kek_env_refuses(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_KEK", "base64...")
    with pytest.raises(StartupValidationError, match="assess_kek: forbidden for api"):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_disabled_clean_api_passes(secrets_dir: Path) -> None:
    app = _state_app()
    await assess_wiring.step_12_assess_check(app, MAIN_DSN)  # type: ignore[arg-type]
    assert app.state.assess_enabled is False
    assert not hasattr(app.state, "assess_engine")


def _api_keys(d: Path) -> None:
    from vektra_assess.keys import KEY_MATRIX, Need, generate

    full = d.parent / "full"
    generate(full)
    for name, spec in KEY_MATRIX.items():
        if spec.api is Need.REQUIRED:
            (d / name).write_bytes((full / name).read_bytes())
            os.chmod(d / name, 0o400)


async def test_enabled_missing_required_key_refuses(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    with pytest.raises(
        StartupValidationError, match="assess_token_signing: required for api"
    ):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_enabled_dsn_logging_in_as_vektra_refuses(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_keys(secrets_dir)
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL", "postgresql+asyncpg://vektra:other@db/vektra"
    )
    with pytest.raises(
        StartupValidationError, match="same user as VEKTRA_DATABASE_URL"
    ):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_enabled_without_dsn_refuses(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_keys(secrets_dir)
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    with pytest.raises(
        StartupValidationError, match="VEKTRA_ASSESS_API_DATABASE_URL is unset"
    ):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_enabled_runs_crypto_selftest(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_keys(secrets_dir)
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    from vektra_assess.crypto import selftest

    def boom() -> None:
        raise StartupValidationError(
            step=selftest.STEP, detail="forced", remediation="-"
        )

    monkeypatch.setattr(selftest, "run_crypto_selftest", boom)
    with pytest.raises(StartupValidationError) as exc:
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]
    assert exc.value.step == "assess_crypto_selftest"


# --- 6.1 role check against a real Postgres (integration) ----------------------


@pytest.fixture(scope="module")
def pg_admin_url():  # type: ignore[no-untyped-def]
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        url = c.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        yield url


async def _exec(url: str, *stmts: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        for s in stmts:
            await conn.execute(text(s))
    await engine.dispose()


def _with_user(url: str, user: str, pw: str) -> str:
    from sqlalchemy.engine import make_url

    return (
        make_url(url)
        .set(username=user, password=pw)
        .render_as_string(hide_password=False)
    )


@pytest.mark.integration
async def test_role_check_against_postgres(
    pg_admin_url: str, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _exec(
        pg_admin_url,
        "CREATE ROLE vektra_assess_api LOGIN PASSWORD 'apipw' NOSUPERUSER NOINHERIT",
        "CREATE ROLE intruder LOGIN PASSWORD 'evil' SUPERUSER",
    )
    _api_keys(secrets_dir)
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")

    # A superuser that is not the main DSN's user is still refused.
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL", _with_user(pg_admin_url, "intruder", "evil")
    )
    with pytest.raises(StartupValidationError, match="'intruder' \\(superuser=True\\)"):
        await assess_wiring.step_12_assess_check(_state_app(), pg_admin_url)  # type: ignore[arg-type]

    # The right role passes and the engine lands on app.state.
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL",
        _with_user(pg_admin_url, "vektra_assess_api", "apipw"),
    )
    app = _state_app()
    await assess_wiring.step_12_assess_check(app, pg_admin_url)  # type: ignore[arg-type]
    try:
        from sqlalchemy import text

        async with app.state.assess_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                    )
                )
            ).one()
        assert tuple(row) == ("vektra_assess_api", False)
        assert "assess_token_signing" in app.state.assess_keys
    finally:
        await app.state.assess_engine.dispose()
