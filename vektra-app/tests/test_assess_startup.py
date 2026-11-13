"""Assess wiring in the API process (plan assess-foundations 6.1, 6.1b, 6.5).

The route table is the evidence for dormancy, not a status field; the key-matrix guard
is driven into refusal with real files; the role check asks a real Postgres who the
session is (integration).
"""

from __future__ import annotations

import asyncio
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
    # these tests check the key matrix, not the production-only settings (erasure mode, UI
    # origin): an EXPLICIT development env, since the default no longer counts for assess
    monkeypatch.setenv("VEKTRA_ENV", "development")
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
    # the assess router, and the discussion UI page and assets mounted with it (plan 9)
    assert _assess_paths(create_app()) == [
        "/api/v1/assess/probe",
        "/api/v1/assess/ui",
        "/api/v1/assess/ui/assets/{name}",
    ]


def test_broken_assess_config_mounts_nothing(
    probe_router: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vektra_app.main import create_app

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "not-a-bool")
    assert _assess_paths(create_app()) == []


def test_an_out_of_bounds_setting_is_named_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan 12b: the API's settings step names the variable and its bound, never the value
    (pydantic's own message echoes the input, and a DSN carries a password)."""
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL", "postgresql+asyncpg://a:s3cr3t@db/x"
    )
    monkeypatch.setenv("VEKTRA_ASSESS_API_POOL_SIZE", "0")
    with pytest.raises(StartupValidationError) as exc:
        assess_wiring.load_assess_settings()
    assert "VEKTRA_ASSESS_API_POOL_SIZE" in exc.value.detail
    assert "s3cr3t" not in exc.value.to_plain_text()
    assert "VEKTRA_ASSESS_API_POOL_SIZE" in exc.value.remediation


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


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_api_refuses_the_smtp_account(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """ARCH-071 / plan 10b: the SMTP account is the worker's; the API refuses to start with
    it mounted or named in its environment, assess enabled or not."""
    if enabled:
        _api_keys(secrets_dir)
        monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    _write_keyring(secrets_dir, "assess_smtp")
    with pytest.raises(StartupValidationError, match="assess_smtp: forbidden for api"):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]
    (secrets_dir / "assess_smtp").chmod(0o600)
    (secrets_dir / "assess_smtp").unlink()
    monkeypatch.setenv("VEKTRA_ASSESS_SMTP_FILE", "/run/secrets/assess_smtp")
    with pytest.raises(StartupValidationError, match="assess_smtp: forbidden for api"):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_api_refuses_the_rag_flag(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Plan assess-teacher-ops / ARCH-072: VEKTRA_ASSESS_RAG_ENABLED=true is refused at
    startup ("not available in this release"), assess enabled or not."""
    if enabled:
        _api_keys(secrets_dir)
        monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_RAG_ENABLED", "true")
    with pytest.raises(StartupValidationError, match="not available in this release"):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_api_refuses_the_reset_notice_flag(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """REQ-092 is out of v1 (product owner 2026-11-08): the notice flag is refused at
    startup like the RAG flag, assess enabled or not."""
    if enabled:
        _api_keys(secrets_dir)
        monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_RESET_NOTICE_ENABLED", "true")
    with pytest.raises(StartupValidationError, match="not available in this release"):
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
        "VEKTRA_ASSESS_API_DATABASE_URL", "postgresql+asyncpg://vektra@db/vektra"
    )
    with pytest.raises(
        StartupValidationError, match="same user as VEKTRA_DATABASE_URL"
    ):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_enabled_dsn_with_password_refuses(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_keys(secrets_dir)
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL",
        "postgresql+asyncpg://vektra_assess_api:pw@db/vektra",
    )
    with pytest.raises(StartupValidationError, match="DSN contains a password"):
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


def _with_user(url: str, user: str) -> str:
    """The DSN as deployed: user and host, no password (it comes from the secret file)."""
    from sqlalchemy.engine import URL, make_url

    u = make_url(url)
    return URL.create(
        u.drivername, user, None, u.host, u.port, u.database
    ).render_as_string(hide_password=False)


@pytest.mark.integration
async def test_role_check_against_postgres(
    pg_admin_url: str, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_keys(secrets_dir)
    pw = (secrets_dir / "assess_db_api_password").read_text().strip()
    await _exec(
        pg_admin_url,
        f"CREATE ROLE vektra_assess_api LOGIN PASSWORD '{pw}' NOSUPERUSER NOINHERIT",
        # same password as the api file, so only the role check can refuse it
        f"CREATE ROLE intruder LOGIN PASSWORD '{pw}' SUPERUSER",
    )
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")

    # A superuser that is not the main DSN's user is still refused.
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL", _with_user(pg_admin_url, "intruder")
    )
    with pytest.raises(StartupValidationError, match="'intruder' \\(superuser=True\\)"):
        await assess_wiring.step_12_assess_check(_state_app(), pg_admin_url)  # type: ignore[arg-type]

    # The right role passes and the engine lands on app.state.
    monkeypatch.setenv(
        "VEKTRA_ASSESS_API_DATABASE_URL",
        _with_user(pg_admin_url, "vektra_assess_api"),
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
        # plan 8 M5.3: one assess-local limiter per route family, the global one never armed
        from vektra_assess.api.state import LIMITER_NAMES

        assert set(app.state.assess_limiters) == set(LIMITER_NAMES)
        assert getattr(app.state, "rate_limiter", None) is None
        # step 12 started the metrics refresher; on this bare server (no assess branch) it
        # polls a function that does not exist, for ever, unless the test stops it
        assert app.state.assess_metrics_task is not None
    finally:
        # as the app's shutdown does: the refresher first, then the engine. Without it the
        # task outlived the test and kept a connection to this module's container polling
        # every 15 s, which held the run up (review of #209)
        await asyncio.wait_for(assess_wiring.stop_metrics_refresher(app), 10)
        assert app.state.assess_metrics_task.done()
        await app.state.assess_engine.dispose()


async def test_step_12_registers_disabled_readiness(secrets_dir: Path) -> None:
    from vektra_admin.health import check_component

    registry = ProviderRegistry()
    await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN, registry)  # type: ignore[arg-type]
    component = await check_component(registry, "assess")
    assert component.status == "disabled"


def test_health_assess_route_reports_disabled(secrets_dir: Path) -> None:
    """GET /health/assess answers 'disabled', not 'no check registered' (6.1b)."""
    import asyncio

    from vektra_app.main import create_app

    app = create_app()
    registry = ProviderRegistry()
    asyncio.run(assess_wiring.step_12_assess_check(app, MAIN_DSN, registry))
    from uuid import uuid4

    from vektra_shared.auth import ApiKeyInfo

    class _Store:
        async def lookup_by_token(self, token: str) -> ApiKeyInfo | None:
            return (
                ApiKeyInfo(key_id=uuid4(), scopes=["admin"]) if token == "adm" else None
            )

    registry.register("key_store", "default", _Store())
    app.state.registry = registry
    app.state.version = "test"
    resp = TestClient(app).get(
        "/health/assess", headers={"Authorization": "Bearer adm"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disabled"


def test_create_app_does_not_arm_the_api_key_rate_limiter():
    """The dormant per-key RPM stays dormant: create_app() sets no rate_limiter (8.3)."""
    from vektra_app.main import create_app

    app = create_app()
    assert getattr(app.state, "rate_limiter", None) is None


# --- the API never sees the parser socket (plan assess-parser, dormant-capability guard) ---


def test_api_refuses_a_parser_socket_path(tmp_path: Path) -> None:
    d = tmp_path / "run"
    d.mkdir()
    assess_wiring.assert_no_parser_surface(d / "parser.sock", d)  # bare dir: fine
    (d / "parser.sock").write_text("")
    with pytest.raises(StartupValidationError, match=r"parser\.sock"):
        assess_wiring.assert_no_parser_surface(d / "parser.sock", d)


def test_api_refuses_a_mounted_socket_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "run"
    d.mkdir()
    monkeypatch.setattr("os.path.ismount", lambda p: str(p) == str(d))
    with pytest.raises(StartupValidationError, match="mounted volume"):
        assess_wiring.assert_no_parser_surface(d / "parser.sock", d)


def test_api_refuses_a_non_empty_socket_dir(tmp_path: Path) -> None:
    d = tmp_path / "run"
    d.mkdir()
    (d / "anything").write_text("")
    with pytest.raises(StartupValidationError, match="not empty"):
        assess_wiring.assert_no_parser_surface(d / "parser.sock", d)


async def test_step_12_runs_the_guard_even_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "run"
    d.mkdir()
    (d / "parser.sock").write_text("")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_SOCKET", str(d / "parser.sock"))
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "false")
    with pytest.raises(StartupValidationError, match="parser socket is reachable"):
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]


async def test_the_pending_turn_driver_starts_with_the_server_and_stops_with_it() -> (
    None
):
    """Plan 8 M4.3: started at the end of the shared startup (so also under ``main()`` with
    uvicorn's lifespan off, BUG-025), only with a discussion runner; stopped at shutdown."""
    import inspect

    from vektra_app import main as app_main

    bare = FastAPI()
    assert assess_wiring.start_assess_background(bare) is False  # assess off: no runner

    app = FastAPI()
    app.state.assess_runner = object()
    assert assess_wiring.start_assess_background(app) is True
    driver = app.state.assess_pending_driver
    assert driver._task is not None and not driver._task.done()
    await assess_wiring.stop_assess_background(app)
    assert driver._task is None and app.state.assess_pending_driver is None
    # both serving paths run the same startup and shutdown, which call these two
    assert "start_assess_background(app)" in inspect.getsource(app_main._run_startup)
    assert "stop_assess_background(app)" in inspect.getsource(app_main._run_shutdown)


def test_assess_limiters_are_local_keyed_by_session_or_student() -> None:
    """Plan 8 M5.3: /state, /turns, /retry and /flag each have their own limiter; keys are the
    hashables the routes pass (a (site, student, activity) tuple, a session id string), counted
    independently of each other and of any API key."""
    import uuid

    from vektra_assess.api.state import LIMITER_NAMES

    limiters = assess_wiring.build_assess_limiters()
    assert (
        tuple(limiters)
        == LIMITER_NAMES
        == (
            "state",
            "turns",
            "retry",
            "flag",
            "ui_page",
            "download",
            "resend",
            "privacy",
        )
    )
    assert len({id(v) for v in limiters.values()}) == 8  # no shared instance
    sid = str(uuid.uuid4())
    assert limiters["turns"].check(sid, 1)[0] is True
    assert limiters["turns"].check(sid, 1)[0] is False
    assert limiters["retry"].check(sid, 1)[0] is True  # another family, its own count
    assert limiters["state"].check((str(uuid.uuid4()), 7, 42), 1)[0] is True


def test_every_route_but_the_discussion_ui_denies_framing(tmp_path: Path) -> None:
    """Plan 9 (ARCH-076): X-Frame-Options: DENY on every response of the API; the assess UI
    page and assets are framed through their own CSP and carry no X-Frame-Options."""
    from vektra_app.main import create_app

    app = create_app()

    import json

    bundle = tmp_path  # removed by pytest (CodeRabbit review 5812178787)
    (bundle / "manifest.json").write_text(
        json.dumps({"js": "assess-ui.abc.js", "css": "assess-ui.abc.css"})
    )
    app.state.assess_ui_static_dir = str(bundle)

    # stand-ins for the UI routes (assess is not mounted here): only the middleware is tested
    @app.get("/api/v1/assess/ui")
    @app.get("/api/v1/assess/ui/probe")
    @app.get("/api/v1/assess/ui/assets/{name}")
    @app.get("/api/v1/assess/ui-other")
    @app.get("/api/v1/assess/uix/probe")
    async def ui_probe() -> dict[str, str]:
        return {"ok": "ui"}

    client = TestClient(app)  # no lifespan: only the middleware stack is exercised
    other = client.get("/openapi.json")
    assert other.headers["x-frame-options"] == "DENY"
    # CodeRabbit 4501067221: only the page and the assets the manifest names are exempt; any
    # other path, under /ui/ or a sibling, keeps DENY
    for denied in (
        "/api/v1/assess/ui/probe",
        "/api/v1/assess/ui-other",
        "/api/v1/assess/uix/probe",
        "/api/v1/assess/ui/assets/not-in-the-manifest.js",
    ):
        assert client.get(denied).headers["x-frame-options"] == "DENY", denied
    for framable in ("/api/v1/assess/ui", "/api/v1/assess/ui/assets/assess-ui.abc.js"):
        r = client.get(framable)
        assert r.status_code == 200 and "x-frame-options" not in r.headers, framable


class _Rows:
    def __init__(self, origins: list[str]) -> None:
        self.origins = origins

    def connect(self):  # type: ignore[no-untyped-def]
        # (id, lms_origin, base_url) per enabled site: the column set, base_url irrelevant
        rows = [
            (f"site-{i}", o, "https://ignored.example/moodle")
            for i, o in enumerate(self.origins)
        ]

        class _Result(list):  # type: ignore[type-arg]
            def all(self):  # type: ignore[no-untyped-def]
                return list(self)

        class _Conn:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return self

            async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
                return False

            async def execute(self, _stmt):  # type: ignore[no-untyped-def]
                return _Result(rows)

        return _Conn()


@pytest.mark.parametrize(
    ("env", "ui", "moodle", "problem"),
    [
        ("production", "https://ui.example", ["https://moodle.example"], None),
        (
            "production",
            "http://ui.example",
            ["https://moodle.example"],
            "ui_origin_not_https",
        ),
        (
            "production",
            "https://m.example",
            ["https://m.example"],
            "ui_origin_equals_lms_origin",
        ),
        # case and the default port are one origin (review #2: one normalisation)
        (
            "production",
            "HTTPS://M.Example:443",
            ["https://m.example"],
            "ui_origin_equals_lms_origin",
        ),
        (
            "production",
            "https://m.example",
            ["HTTPS://M.EXAMPLE:443"],
            "ui_origin_equals_lms_origin",
        ),
        # CodeRabbit 4501070007: an unusable stored origin refuses startup, never skipped
        (
            "production",
            "https://ui.example",
            ["https://m.example/path"],
            "lms_origin_invalid",
        ),
        ("development", "http://localhost:8000", ["http://127.0.0.1:8080"], None),
        (
            "development",
            "http://localhost:8000",
            ["http://localhost:8000"],
            "ui_origin_equals_lms_origin",
        ),
    ],
)
async def test_startup_refuses_a_ui_origin_not_distinct_or_not_https(
    env: str,
    ui: str,
    moodle: list[str],
    problem: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan 9 (ARCH-076): the API refuses a UI origin that is not https outside development
    or equals a registered site's Moodle origin."""
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", env)
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", ui)
    settings = VektraAssessSettings()
    if problem is None:
        await assess_wiring.check_ui_origin(_Rows(moodle), settings)
    else:
        with pytest.raises(StartupValidationError, match=problem):
            await assess_wiring.check_ui_origin(_Rows(moodle), settings)


@pytest.mark.parametrize(
    "bad",
    [
        "https://ui.example/path",
        "https://*.example",
        "ftp://ui.example",
        "https://u:p@ui.example",
        "ui.example",
    ],
)
def test_the_ui_origin_setting_is_a_bare_origin(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", bad)
    with pytest.raises(ValidationError):
        VektraAssessSettings()
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", "HTTPS://UI.Example:8443/")
    assert VektraAssessSettings().ui_origin == "https://ui.example:8443"


def test_a_malformed_ui_origin_only_warns_while_assess_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #7: a dormant feature's setting never stops the API from starting."""
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "false")
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", "https://ui.example/path")
    with pytest.warns(UserWarning, match="VEKTRA_ASSESS_UI_ORIGIN"):
        loaded = VektraAssessSettings()
    assert loaded.ui_origin == "https://ui.example/path"  # kept as given, never used


@pytest.mark.parametrize(
    ("env", "refused"), [("production", True), ("development", False)]
)
async def test_an_unset_ui_origin_is_refused_outside_development(
    env: str, refused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1: with assess enabled the UI origin is the precondition of the iframe's
    allow-same-origin sandbox; unset is refused outside development (development: the page
    answers 503 until it is set)."""
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", env)
    monkeypatch.delenv("VEKTRA_ASSESS_UI_ORIGIN", raising=False)
    settings = VektraAssessSettings()
    if refused:
        with pytest.raises(StartupValidationError, match="ui_origin_unset"):
            await assess_wiring.check_ui_origin(_Rows([]), settings)
    else:
        await assess_wiring.check_ui_origin(_Rows([]), settings)


async def test_a_site_without_a_usable_origin_is_named_with_its_own_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeRabbit 4501246170: the site's problem names the site and points at
    assess_sites.lms_origin (or disabling the site), not at the UI origin."""
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", "production")
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", "https://ui.example")
    with pytest.raises(StartupValidationError) as refused:
        await assess_wiring.check_ui_origin(
            _Rows(["https://moodle.example", "https://m.example/path"]),
            VektraAssessSettings(),
        )
    err = refused.value
    assert "site-1" in err.detail and "lms_origin_invalid" in err.detail
    assert "VEKTRA_ASSESS_UI_ORIGIN" not in err.detail
    assert "assess_sites.lms_origin" in err.remediation
    assert "disable" in err.remediation


async def test_a_ui_origin_problem_keeps_the_ui_origin_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", "production")
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", "https://m.example")
    with pytest.raises(StartupValidationError) as refused:
        await assess_wiring.check_ui_origin(
            _Rows(["https://m.example"]), VektraAssessSettings()
        )
    err = refused.value
    assert err.detail == "VEKTRA_ASSESS_UI_ORIGIN refused: ui_origin_equals_lms_origin"
    assert "own https origin" in err.remediation


async def test_a_site_on_http_is_named_with_its_own_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeRabbit review 5812478913 (assess_wiring.py 209-220): outside development a site whose
    Moodle origin is http is the SITE's problem, reported with its id and its own remedy."""
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ENV", "production")
    monkeypatch.setenv("VEKTRA_ASSESS_UI_ORIGIN", "https://ui.example")
    with pytest.raises(StartupValidationError) as refused:
        await assess_wiring.check_ui_origin(
            _Rows(["https://moodle.example", "http://m.example"]),
            VektraAssessSettings(),
        )
    err = refused.value
    assert err.detail == "assess site site-1 refused: lms_origin_not_https"
    assert "https" in err.remediation and "disable" in err.remediation
    assert "VEKTRA_ASSESS_UI_ORIGIN" not in err.remediation


async def test_the_metrics_refresher_feeds_the_process_collector() -> None:
    """Plan 12b: step 12 attaches a snapshot cache to the /metrics collector and refreshes it
    in the background; shutdown cancels the task."""
    import asyncio

    from prometheus_client import generate_latest

    class _Conn:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
            return False

        async def execute(self, *_a, **_k):  # type: ignore[no-untyped-def]
            class _R:
                def scalar_one(self):  # type: ignore[no-untyped-def]
                    return {"sessions": {"ACTIVE": 3}}

            return _R()

    class _Engine:
        def connect(self):  # type: ignore[no-untyped-def]
            return _Conn()

    app = _state_app()
    assess_wiring.start_metrics_refresher(app, _Engine())  # type: ignore[arg-type]
    try:
        for _ in range(50):
            if app.state.assess_metrics_cache.snapshot is not None:
                break
            await asyncio.sleep(0.01)
        text = generate_latest().decode()
        assert 'vektra_assess_sessions{state="ACTIVE"} 3.0' in text
        assert "vektra_assess_metrics_up 1.0" in text
    finally:
        await assess_wiring.stop_metrics_refresher(app)
        # review #10: awaited, not only cancelled, before the engine is disposed
        assert app.state.assess_metrics_task.done()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "carrier", ["ASSESS_STUB_LMS_TOKENS", "VEKTRA_ASSESS_STUB_LMS_TOKENS_FILE"]
)
async def test_the_api_refuses_an_env_var_carrying_a_providers_secret(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, carrier: str
) -> None:
    """Review of #207, finding 1 (ARCH-066): the names come from the installed providers'
    entry points (``REQUIRED_SECRETS``), read without building any provider; assess enabled
    or not, the API refuses a variable that carries one."""
    import sys

    tests = str(Path(__file__).resolve().parent)
    if tests not in sys.path:
        sys.path.insert(0, tests)
    import lms_stub

    lms_stub.install(monkeypatch.setattr)
    lms_stub.StubLmsProvider.built = []
    if enabled:
        _api_keys(secrets_dir)
        monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv(carrier, "a-token")
    with pytest.raises(StartupValidationError) as exc:
        await assess_wiring.step_12_assess_check(_state_app(), MAIN_DSN)  # type: ignore[arg-type]
    assert (
        f"{lms_stub.STUB_SECRET}: forbidden for api but carried by env var(s) ['{carrier}']"
        in exc.value.detail
    )
    assert "a-token" not in exc.value.detail
    assert lms_stub.StubLmsProvider.built == []
