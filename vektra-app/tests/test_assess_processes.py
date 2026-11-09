"""Assess worker and parser entrypoints (plan assess-foundations 6.1b, 6.2, 6.3, 6.5).

The entrypoints are run as real subprocesses where the claim is about the process
(exit codes when disabled); the checks are driven into refusal with real files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vektra_app import assess_parser, assess_worker
from vektra_shared.startup import StartupValidationError


def _run_module(module: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")}
    return subprocess.run(
        [sys.executable, "-m", module],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _worker_keys(d: Path, extra: set[str] = frozenset()) -> Path:  # type: ignore[assignment]
    from vektra_assess.keys import KEY_MATRIX, Need, generate

    full = d.parent / f"full-{d.name}"
    generate(full)
    d.mkdir()
    for name, spec in KEY_MATRIX.items():
        if spec.worker is Need.REQUIRED or name in extra:
            (d / name).write_bytes((full / name).read_bytes())
            os.chmod(d / name, 0o400)
    return d


# --- disabled: both entrypoints refuse to run (6.1b) --------------------------


@pytest.mark.parametrize(
    "module", ["vektra_app.assess_worker", "vektra_app.assess_parser"]
)
def test_entrypoint_exits_nonzero_when_disabled(module: str) -> None:
    res = _run_module(module, {})
    assert res.returncode == 2, res.stderr
    assert "VEKTRA_ASSESS_ENABLED is not true" in res.stderr


def test_worker_entry_does_not_import_the_api() -> None:
    code = (
        "import sys, vektra_app.assess_worker, vektra_app.assess_parser; "
        "bad=[m for m in ('vektra_app.main','vektra_index','vektra_ingest','torch') if m in sys.modules]; "
        "print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.stdout.strip() == "[]", out.stderr


# --- worker checks (6.2, 6.5) -------------------------------------------------


def test_worker_refuses_with_torch_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", object())
    with pytest.raises(
        StartupValidationError, match=r"forbidden modules loaded .*'torch'"
    ):
        assess_worker.assert_import_ceiling()


async def test_worker_run_checks_hits_ceiling_before_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv(
        "VEKTRA_ASSESS_WORKER_DATABASE_URL", "postgresql+asyncpg://w@127.0.0.1:1/db"
    )
    monkeypatch.setitem(sys.modules, "torch", object())
    with pytest.raises(StartupValidationError, match="torch"):
        await assess_worker.run_checks()


@pytest.mark.parametrize(
    "flag", ["VEKTRA_ASSESS_RAG_ENABLED", "VEKTRA_ASSESS_RESET_NOTICE_ENABLED"]
)
async def test_worker_refuses_the_flags_not_in_this_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """CodeRabbit nitpick (PR #179): the worker, not only the API, refuses to start with the
    RAG flag or the REQ-092 reset-notice flag on."""
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv(flag, "true")
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not available in this release"):
        await assess_worker.run_checks()


async def test_worker_refuses_api_only_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w", {"assess_token_signing"})
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    with pytest.raises(
        StartupValidationError, match="assess_token_signing: forbidden for worker"
    ):
        await assess_worker.run_checks()


async def test_worker_refuses_main_dsn_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv("VEKTRA_DATABASE_URL", "postgresql+asyncpg://vektra:a@db/vektra")
    monkeypatch.setenv(
        "VEKTRA_ASSESS_WORKER_DATABASE_URL", "postgresql+asyncpg://vektra@db/vektra"
    )
    with pytest.raises(StartupValidationError, match="same user"):
        await assess_worker.run_checks()


# --- parser (6.3) --------------------------------------------------------------


def test_parser_refuses_secrets_and_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    d = tmp_path / "s"
    d.mkdir()
    (d / "assess_kek").write_text("{}")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    with pytest.raises(
        StartupValidationError, match="assess_kek: forbidden for parser"
    ):
        assess_parser.check_environment()
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_DATABASE_URL", "postgresql://x")
    with pytest.raises(
        StartupValidationError,
        match="VEKTRA_ASSESS_WORKER_DATABASE_URL must not be set",
    ):
        assess_parser.check_environment()


def test_parser_import_ceiling_in_a_fresh_process() -> None:
    """What the parser process really holds after importing its serving code."""
    code = (
        "import sys; from vektra_app import assess_parser; "
        "from vektra_assess.parser_sidecar import server; "
        "assess_parser.assert_import_ceiling(); print('ceiling ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.stdout.strip() == "ceiling ok", out.stderr


def test_parser_ceiling_refuses_forbidden_and_stray_modules() -> None:
    for bad in ("sqlalchemy", "httpx", "torch", "vektra_shared.db", "vektra_index.x"):
        with pytest.raises(StartupValidationError, match="forbidden modules"):
            assess_parser.assert_import_ceiling(["vektra_assess.parser_protocol", bad])
    for stray in (
        "vektra_assess.worker",
        "vektra_assess.moodle.client",
        "vektra_assess.crypto",
    ):
        with pytest.raises(StartupValidationError, match="forbidden modules"):
            assess_parser.assert_import_ceiling([stray])
    assess_parser.assert_import_ceiling(
        [
            "vektra_assess",
            "vektra_assess.config",
            "vektra_assess.keys",
            "vektra_assess.parser_sidecar.server",
            "vektra_assess.parser_protocol",
        ]
    )


def test_server_config_has_one_source_the_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_POOL", "5")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_TIMEOUT_S", "7.5")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_MAX_CHARS", "5000")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_MAX_PAGES", "10")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_MAX_FILE_BYTES", "2048")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB", "300")
    monkeypatch.setenv("VEKTRA_ASSESS_PARSER_SOCKET", str(tmp_path / "s.sock"))
    cfg = assess_parser.build_server_config(VektraAssessSettings())
    assert (cfg.pool, cfg.timeout_s, cfg.max_chars, cfg.max_pages) == (5, 7.5, 5000, 10)
    assert (cfg.max_file_bytes, cfg.rlimit_as) == (2048, 300 * 1024 * 1024)
    assert cfg.socket_path == tmp_path / "s.sock"


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("VEKTRA_ASSESS_PARSER_POOL", "abc"),
        ("VEKTRA_ASSESS_PARSER_POOL", "99"),
        ("VEKTRA_ASSESS_PARSER_TIMEOUT_S", "fast"),
        ("VEKTRA_ASSESS_PARSER_MAX_PAGES", "0"),
    ],
)
def test_malformed_parser_settings_exit_cleanly_not_with_a_traceback(
    var: str, value: str
) -> None:
    res = _run_module(
        "vektra_app.assess_parser", {"VEKTRA_ASSESS_ENABLED": "true", var: value}
    )
    assert res.returncode == 2, res.stderr
    assert var in res.stderr and "Traceback" not in res.stderr


def test_api_entry_does_not_import_the_parser_entry() -> None:
    code = (
        "import sys, vektra_app.main; "
        "print([m for m in sys.modules if m.startswith(('vektra_app.assess_parser', "
        "'vektra_assess.parser_sidecar'))])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300
    )
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# --- worker positive path against a real Postgres (integration) ---------------


@pytest.mark.integration
async def test_worker_checks_against_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker not available: {exc}")
    from sqlalchemy import text
    from sqlalchemy.engine import URL, make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        admin = c.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        d = _worker_keys(tmp_path / "w")
        pw = (d / "assess_db_worker_password").read_text().strip()
        eng = create_async_engine(admin, isolation_level="AUTOCOMMIT")
        async with eng.connect() as conn:
            await conn.execute(
                text(
                    f"CREATE ROLE vektra_assess_worker LOGIN PASSWORD '{pw}' NOSUPERUSER NOINHERIT"
                )
            )
        await eng.dispose()
        u = make_url(admin)
        worker_url = URL.create(
            u.drivername, "vektra_assess_worker", None, u.host, u.port, u.database
        )
        monkeypatch.setenv(
            "VEKTRA_ASSESS_WORKER_DATABASE_URL",
            worker_url.render_as_string(hide_password=False),
        )
        # In a fresh process: the import ceiling is process-wide, and this pytest
        # session has long since imported vektra_index through other tests.
        code = (
            "import asyncio\n"
            "from sqlalchemy import text\n"
            "import vektra_app.assess_worker as w\n"
            "async def m():\n"
            "    e = await w.run_checks()\n"
            "    async with e.connect() as c:\n"
            "        print((await c.execute(text('SELECT current_user'))).scalar())\n"
            "    await e.dispose()\n"
            "asyncio.run(m())\n"
        )
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")},
            "VEKTRA_ASSESS_ENABLED": "true",
            "VEKTRA_ASSESS_SECRETS_DIR": str(d),
            "VEKTRA_DATABASE_URL": admin,
            "VEKTRA_ASSESS_WORKER_DATABASE_URL": worker_url.render_as_string(
                hide_password=False
            ),
            # plan 10b: a production worker needs its relay (checked before the database)
            "VEKTRA_ASSESS_SMTP_HOST": "relay.example.org",
            "VEKTRA_ASSESS_SMTP_FROM": "vektra@example.org",
            "VEKTRA_ASSESS_OPERATOR_EMAIL": "ops@example.org",
        }
        res = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "vektra_assess_worker"


# --- docker/entrypoint.sh targets (6.4) ----------------------------------------

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def _entry(tmp_path: Path, target: str) -> tuple[int, str]:
    """Run the entrypoint with stub python/alembic that log their argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for tool in ("python", "alembic"):
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        stub.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    res = subprocess.run(
        ["sh", str(ENTRYPOINT), target], env=env, capture_output=True, text=True
    )
    return res.returncode, (log.read_text() if log.exists() else "") + res.stderr


@pytest.mark.parametrize(
    ("target", "module"),
    [
        ("assess-worker", "vektra_app.assess_worker"),
        ("assess-parser", "vektra_app.assess_parser"),
    ],
)
def test_assess_targets_exec_their_module_without_migrations(
    tmp_path: Path, target: str, module: str
) -> None:
    code, calls = _entry(tmp_path, target)
    assert code == 0
    assert calls.strip() == f"python -m {module}"
    assert "alembic" not in calls


def test_no_generic_worker_target(tmp_path: Path) -> None:
    code, out = _entry(tmp_path, "worker")
    assert code == 1
    assert "assess-worker, assess-parser" in out


async def test_worker_refuses_plain_http_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv("VEKTRA_ASSESS_MOODLE_ALLOW_HTTP", "true")
    monkeypatch.setenv("VEKTRA_ENV", "production")
    with pytest.raises(StartupValidationError, match="only honoured with"):
        await assess_worker.run_checks()


# --- job loop composition (plan assess-job-loop, group C) ---------------------


def _checked(monkeypatch: pytest.MonkeyPatch, **env: str):  # type: ignore[no-untyped-def]
    """A ``Checked`` over a lazy engine: composition never connects until it is started."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    url = "postgresql+asyncpg://vektra_assess_worker:x@127.0.0.1:1/db"
    return assess_worker.Checked(create_async_engine(url), VektraAssessSettings(), url)


def test_unbound_slots_refuse_to_compose_with_the_owner_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(monkeypatch)
    with pytest.raises(StartupValidationError) as exc:
        assess_worker._compose(checked)
    text = exc.value.to_plain_text()
    assert "job:transcript_generate" in text and "tick:purge" in text
    assert "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND" in text


def test_development_override_composes_with_only_framework_slots_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _checked(monkeypatch, VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true")
    _runtime, scheduler, heartbeat, control, _acquire, _audit = assess_worker._compose(
        checked
    )
    assert heartbeat.state == "starting"
    assert not control.connected
    # the heartbeat and the cleanup are the only bodies this plan writes
    unbound = {t.name for t in scheduler._ticks if not t.bound}
    assert not ({"heartbeat", "job_cleanup", "lease_reaper"} & unbound)
    assert {"purge", "setup_check"} <= unbound  # the session ticks are bound (plan 8)
    assert "sweeper" not in unbound
    assert "outage_probe" not in unbound  # M4.2


async def test_unbound_override_is_refused_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv("VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND", "true")
    monkeypatch.setenv("VEKTRA_ENV", "production")
    with pytest.raises(
        StartupValidationError, match="ALLOW_UNBOUND=true is only honoured"
    ):
        await assess_worker.run_checks()


def test_the_worker_keys_bind_the_preflight_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan assess-session-lifecycle M1.5: with the worker's keys the preflight slot is bound;
    without them (no key matrix passed) it stays unbound and startup refuses."""
    import os

    from vektra_assess.jobs import JobKind
    from vektra_assess.keys import AssessKeyring

    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_LLM_MODEL="openai/test-model",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keyed = assess_worker.Checked(
        checked.engine,
        checked.settings,
        checked.worker_url,
        {name: ring for name in assess_worker.SESSION_KEYS},
    )
    runtime, *_ = assess_worker._compose(keyed)
    bound = {k for k, spec in runtime._handlers.items() if spec.bound}
    assert JobKind.PREFLIGHT in bound
    runtime, *_ = assess_worker._compose(checked)
    assert JobKind.PREFLIGHT not in {
        k for k, spec in runtime._handlers.items() if spec.bound
    }


async def test_privacy_override_is_refused_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _worker_keys(tmp_path / "w")
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv("VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET", "true")
    monkeypatch.setenv("VEKTRA_ENV", "production")
    with pytest.raises(
        StartupValidationError, match="PRIVACY_ALLOW_UNSET=true is only"
    ):
        await assess_worker.run_checks()


def test_invalid_job_policy_refuses_to_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_JOB_PURGE_BUDGET_S="60",
        VEKTRA_ASSESS_JOB_PURGE_LEASE_S="100",
    )
    with pytest.raises(StartupValidationError, match="at least 2 x budget_s"):
        assess_worker._compose(checked)


def test_one_pool_serves_the_worker_within_the_connection_budget() -> None:
    engine = assess_worker.make_engine(
        "postgresql+asyncpg://vektra_assess_worker:x@127.0.0.1:1/db", 5
    )
    pool = engine.pool
    assert (pool.size(), pool._max_overflow) == (5, 0)
    assert pool._timeout == 15


def test_asyncpg_dsn_drops_the_sqlalchemy_driver() -> None:
    assert (
        assess_worker._asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/d")
        == "postgresql://u:p@h:5432/d"
    )


def test_check_imports_flag_loads_the_whole_composition_under_the_ceiling() -> None:
    """The plan's ``--check-imports``: the worker's real import set, in a fresh process."""
    out = subprocess.run(
        [sys.executable, "-m", "vektra_app.assess_worker", "--check-imports"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ceiling ok"


def test_the_ceiling_also_forbids_the_vektra_api_stack() -> None:
    for module in ("uvicorn", "vektra_learn"):
        with pytest.raises(StartupValidationError, match=module):
            assess_worker.assert_import_ceiling([module])
    # fastapi arrives with litellm (a third-party import), so it cannot be forbidden
    assess_worker.assert_import_ceiling(["fastapi", "starlette"])


async def test_notify_self_test_failure_refuses_to_start_naming_the_pooler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: the worker refuses to start when the NOTIFY self-test fails."""
    import asyncio
    from types import SimpleNamespace

    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.worker.listener import ControlConnectionError

    class Control:
        async def start(self) -> None:
            raise ControlConnectionError(
                "LISTEN/NOTIFY self-test failed: is the worker DSN behind a transaction-mode pooler?"
            )

    parts = (None, None, None, Control(), None, None)
    monkeypatch.setattr(assess_worker, "_compose", lambda checked, bindings=None: parts)
    with pytest.raises(StartupValidationError, match="transaction-mode pooler"):
        await assess_worker._serve(  # type: ignore[arg-type]
            SimpleNamespace(settings=VektraAssessSettings()), asyncio.Event()
        )


async def test_startup_that_never_finishes_is_refused_with_a_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding: a stuck statement before 'ready' must not hang the process."""
    import asyncio
    from types import SimpleNamespace

    from vektra_assess.config import VektraAssessSettings

    class Control:
        async def start(self) -> None:
            await asyncio.sleep(3600)

        async def stop(self) -> None:
            return None

    parts = (None, None, None, Control(), None, None)
    monkeypatch.setattr(assess_worker, "_compose", lambda checked, bindings=None: parts)
    settings = SimpleNamespace(
        worker_wedge_exit_s=0.2, worker_drain_s=1.0
    )  # a short window for the test
    _ = VektraAssessSettings  # the real settings class validates the minimum of 10 s
    with pytest.raises(StartupValidationError, match="startup did not complete within"):
        await assess_worker._serve(  # type: ignore[arg-type]
            SimpleNamespace(settings=settings), asyncio.Event()
        )


def test_the_ws_token_keyring_binds_the_setup_check_handler_and_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The web-service plan, task F4 (carried over to assess-job-loop): kind ``setup_check`` in
    its own pool with the 120 s hard timeout, plus the daily tick that also runs on startup; the
    parser's ``parser_sidecar_running`` item is part of the registry the handler runs."""
    from vektra_assess.jobs import JobKind
    from vektra_assess.keys import Credentials
    from vektra_assess.worker import setup_items

    # the setup-check module of vektra-assess, reached through the worker's own items module
    check_mod = sys.modules[setup_items.builtin_registry.__module__]
    pluggable_items = check_mod.PLUGGABLE_ITEMS

    checked = _checked(monkeypatch, VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true")
    keyed = assess_worker.Checked(
        checked.engine,
        checked.settings,
        checked.worker_url,
        {"assess_ws_tokens": Credentials("assess_ws_tokens", {})},
    )
    runtime, scheduler, *_ = assess_worker._compose(keyed)
    spec = runtime._handlers[JobKind.SETUP_CHECK]
    assert spec.bound and spec.pool == "setup" and spec.budget_s == 120
    assert spec.lease_s >= 2 * spec.budget_s
    assert "setup" in runtime._pools
    tick = next(t for t in scheduler._ticks if t.name == "setup_check")
    assert tick.bound and tick.run_on_startup and tick.interval_s == 86_400
    parts = assess_worker._setup_check_parts(keyed)
    # every pluggable item has its contributor (none is MISSING as not_registered)
    assert set(pluggable_items) <= set(parts.registry.names())
    # two compositions do not register into one shared registry (it would raise)
    assess_worker._setup_check_parts(keyed)
    # without the key matrix nothing of the setup check is bound
    runtime, scheduler, *_ = assess_worker._compose(checked)
    assert not runtime._handlers[JobKind.SETUP_CHECK].bound
    assert not next(t for t in scheduler._ticks if t.name == "setup_check").bound


def test_one_ws_client_serves_every_ws_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 3: the session handlers and the setup check share ONE WS client, so one
    token bucket (``VEKTRA_ASSESS_WS_RPS`` is a per-worker cap), one breaker registry and one
    connection pool; a second client would double the cap and keep the breakers apart."""
    import os

    from vektra_assess.keys import AssessKeyring, Credentials

    made: list[object] = []
    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_LLM_MODEL="openai/test-model",
    )
    # the WS client class and its module, as the composition builds it (the vektra-assess
    # names are not repeated here)
    ws_client_cls = type(assess_worker._ws_client(checked))
    client_mod = sys.modules[ws_client_cls.__module__]

    class Counting(ws_client_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(client_mod, ws_client_cls.__name__, Counting)
    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_LLM_MODEL="openai/test-model",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {name: ring for name in assess_worker.SESSION_KEYS}
    keys["assess_ws_tokens"] = Credentials("assess_ws_tokens", {})
    full = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    assess_worker._compose(full)
    assert len(made) == 1


def test_the_smtp_item_gets_the_probe_tick_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 1: the setup-check item ``smtp_probe`` can run the same bounded probe the
    tick runs, so a missing or stale row is probed instead of failing for a day."""
    import os

    from vektra_assess.keys import AssessKeyring, Credentials
    from vektra_assess.worker import setup_items

    got: dict[str, object] = {}
    real = setup_items.register_worker_items

    def spy(*a: object, **k: object) -> None:
        got.update(k)
        real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(setup_items, "register_worker_items", spy)
    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_SMTP_HOST="relay.uni.example",
        VEKTRA_ASSESS_SMTP_FROM="vektra@uni.example",
        VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {
        "assess_transcript_kek": ring,
        "assess_teacher_config_private": ring,
        "assess_smtp": Credentials("assess_smtp", {}),
        "assess_ws_tokens": Credentials("assess_ws_tokens", {}),
    }
    full = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    _runtime, scheduler, *_ = assess_worker._compose(full)
    tick = next(t for t in scheduler._ticks if t.name == "smtp_probe")
    assert got["smtp_configured"] is True
    assert got["smtp_probe"] is tick.body is not None


def _plan_statuses() -> dict[str, str]:
    """plan file stem -> status cell, from the plan table of .s2s/plans/INDEX-ASSESS.md."""
    import re

    index = Path(__file__).resolve().parents[2] / ".s2s" / "plans" / "INDEX-ASSESS.md"
    out: dict[str, str] = {}
    for line in index.read_text().splitlines():
        m = re.match(
            r"\|[^|]*\|\s*\[[^\]]*\]\(([^)]*?)\.md\)\s*\|.*\|\s*([^|]*?)\s*\|$", line
        )
        if m:
            out[Path(m.group(1)).name] = m.group(2)
    return out


def test_the_unbound_production_slots_are_exactly_the_documented_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard (2026-11-08): the setup_check binding was "carried over" between two completed plans
    and bound by neither, so the production worker could not start. The real composition, with
    every worker key and a relay, may leave unbound only the slots of plans still pending in
    INDEX-ASSESS and the open decisions recorded in ``slots.OPEN_DECISIONS``."""
    import os

    from vektra_assess.keys import AssessKeyring, Credentials
    from vektra_assess.worker import slots

    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_LLM_MODEL="openai/test-model",
        VEKTRA_ASSESS_SMTP_HOST="relay.uni.example",
        VEKTRA_ASSESS_SMTP_FROM="vektra@uni.example",
        VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example",
        VEKTRA_ASSESS_OPERATOR_EMAIL="ops@uni.example",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {name: ring for name in assess_worker.SESSION_KEYS}
    keys |= {
        "assess_smtp": Credentials("assess_smtp", {}),
        "assess_ws_tokens": Credentials("assess_ws_tokens", {}),
    }
    full = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    runtime, scheduler, *_ = assess_worker._compose(full)
    unbound = {f"job:{k.value}" for k, s in runtime._handlers.items() if not s.bound}
    unbound |= {f"tick:{t.name}" for t in scheduler._ticks if not t.bound}
    assert unbound == slots.expected_unbound(), (
        f"unbound but not allowlisted: {sorted(unbound - slots.expected_unbound())}; "
        f"allowlisted but bound: {sorted(slots.expected_unbound() - unbound)}"
    )

    statuses = _plan_statuses()
    for plan in slots.PENDING_OWNER_PLANS:
        assert plan in statuses, plan
        assert not statuses[plan].startswith("completed"), (
            f"{plan} is completed: its slots must be bound, not allowlisted"
        )
    owners = {f"job:{k.value}": o for k, o in slots.HANDLER_OWNERS.items()}
    owners |= {f"tick:{n}": o for n, o in slots.TICK_OWNERS.items()}
    for slot in slots.OPEN_DECISIONS:
        # an open decision is only for a slot whose owner is done; otherwise it is just pending
        assert statuses[owners[slot]].startswith("completed"), slot


# --- transcript delivery (plan assess-transcript-delivery 10b) ----------------------------


def test_the_worker_refuses_to_run_without_a_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess.config import VektraAssessSettings

    with pytest.raises(StartupValidationError, match="VEKTRA_ASSESS_SMTP_HOST"):
        assess_worker.assert_smtp_configured(VektraAssessSettings())
    relay = {
        "VEKTRA_ASSESS_SMTP_HOST": "relay.uni.example",
        "VEKTRA_ASSESS_SMTP_FROM": "vektra@uni.example",
    }
    # the operator alerts (REQ-071) need their address: refused by name, before the database
    with pytest.raises(StartupValidationError, match="VEKTRA_ASSESS_OPERATOR_EMAIL"):
        assess_worker.assert_smtp_configured(VektraAssessSettings.model_validate(relay))
    assess_worker.assert_smtp_configured(
        VektraAssessSettings.model_validate(
            {**relay, "VEKTRA_ASSESS_OPERATOR_EMAIL": "ops@uni.example"}
        )
    )
    # development override: the delivery slots stay unbound instead
    assess_worker.assert_smtp_configured(
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND": True}
        )
    )


@pytest.mark.parametrize(
    "data",
    [
        {"username": "u"},
        {"password": "p"},
        {"username": "", "password": "p"},
        {"username": 1, "password": "p"},
    ],
)
def test_the_smtp_account_is_both_fields_or_none(data: dict[str, object]) -> None:
    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.keys import Credentials

    settings = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_SMTP_HOST": "relay", "VEKTRA_ASSESS_SMTP_FROM": "v@uni.example"}
    )
    with pytest.raises(StartupValidationError, match="assess_smtp"):
        assess_worker.smtp_config(settings, Credentials("assess_smtp", data))
    none = assess_worker.smtp_config(settings, Credentials("assess_smtp", {}))
    assert none.username is None and none.port == 587 and none.tls_mode == "starttls"
    both = assess_worker.smtp_config(
        settings,
        Credentials("assess_smtp", {"username": "u", "password": "s3cr3t-relay-pw"}),
    )
    assert (both.username, both.password) == ("u", "s3cr3t-relay-pw")
    assert "s3cr3t-relay-pw" not in repr(both)


def test_a_configured_relay_binds_the_delivery_handler_and_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from vektra_assess.jobs import JobKind
    from vektra_assess.keys import AssessKeyring, Credentials

    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_SMTP_HOST="relay.uni.example",
        VEKTRA_ASSESS_SMTP_FROM="vektra@uni.example",
        VEKTRA_ASSESS_RECIPIENT_DOMAINS="uni.example",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {
        "assess_transcript_kek": ring,
        "assess_teacher_config_private": ring,
        "assess_smtp": Credentials("assess_smtp", {}),
    }
    keyed = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    runtime, scheduler, *_ = assess_worker._compose(keyed)
    assert runtime._handlers[JobKind.TRANSCRIPT_DELIVER].bound
    bound = {t.name for t in scheduler._ticks if t.bound}
    assert {"delivery_retry", "smtp_probe"} <= bound
    # the teacher notification rides the same relay; the operator one needs its address
    assert runtime._handlers[JobKind.TEACHER_NOTIFY].bound
    assert not runtime._handlers[JobKind.OPERATOR_NOTIFY].bound
    with_ops = _checked(monkeypatch, VEKTRA_ASSESS_OPERATOR_EMAIL="ops@uni.example")
    runtime, *_ = assess_worker._compose(
        assess_worker.Checked(
            with_ops.engine, with_ops.settings, with_ops.worker_url, keys
        )
    )
    assert runtime._handlers[JobKind.OPERATOR_NOTIFY].bound
    assert runtime._handlers[JobKind.TEACHER_NOTIFY].bound
    # no relay configured: nothing of the delivery is bound
    runtime, scheduler, *_ = assess_worker._compose(checked)
    assert not runtime._handlers[JobKind.TRANSCRIPT_DELIVER].bound
    assert not runtime._handlers[JobKind.TEACHER_NOTIFY].bound
    assert not {"delivery_retry", "smtp_probe"} & {
        t.name for t in scheduler._ticks if t.bound
    }


def test_the_delivery_budget_must_cover_the_worst_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #5: bounded slot wait + send timeout + QUIT bound + a DB margin must fit the
    transcript_deliver budget, or the worker refuses to compose."""
    import os

    from vektra_assess.keys import AssessKeyring, Credentials

    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_SMTP_HOST="relay.uni.example",
        VEKTRA_ASSESS_SMTP_FROM="vektra@uni.example",
        VEKTRA_ASSESS_SMTP_TIMEOUT_S="30",
        VEKTRA_ASSESS_JOB_TRANSCRIPT_DELIVER_BUDGET_S="60",
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {
        "assess_transcript_kek": ring,
        "assess_teacher_config_private": ring,
        "assess_smtp": Credentials("assess_smtp", {}),
    }
    keyed = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    with pytest.raises(StartupValidationError, match="transcript_deliver budget 60 s"):
        assess_worker._compose(keyed)


@pytest.mark.parametrize("kind", ["teacher_notify", "operator_notify"])
def test_the_notify_budget_must_cover_the_worst_send(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The notification emails share the relay's worst case: a budget shorter than one send
    would cut every attempt off mid-session, so the worker refuses to compose."""
    import os

    from vektra_assess.keys import AssessKeyring, Credentials

    checked = _checked(
        monkeypatch,
        VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND="true",
        VEKTRA_ASSESS_SMTP_HOST="relay.uni.example",
        VEKTRA_ASSESS_SMTP_FROM="vektra@uni.example",
        VEKTRA_ASSESS_OPERATOR_EMAIL="ops@uni.example",
        **{f"VEKTRA_ASSESS_JOB_{kind.upper()}_BUDGET_S": "30"},
    )
    ring = AssessKeyring("k", "k1", {"k1": os.urandom(32)})
    keys: dict[str, object] = {
        "assess_transcript_kek": ring,
        "assess_teacher_config_private": ring,
        "assess_smtp": Credentials("assess_smtp", {}),
    }
    keyed = assess_worker.Checked(
        checked.engine, checked.settings, checked.worker_url, keys
    )
    with pytest.raises(StartupValidationError, match=f"{kind} budget 30 s"):
        assess_worker._compose(keyed)


@pytest.mark.parametrize(
    "extra",
    [{}, {"VEKTRA_ASSESS_SMTP_FROM": "vektra@uni.example"}],
    ids=["defaults", "smtp-from-set"],
)
def test_the_parser_loads_its_settings_under_the_ceiling(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    """CodeRabbit round 2 (config.py): validating the settings must not import a module the
    parser's ceiling refuses (the delivery schedule and address rules)."""
    code = (
        "from vektra_app import assess_parser as p; p.check_environment(); "
        "from vektra_assess.parser_sidecar import server; "
        "p.assert_import_ceiling(); print('ceiling ok')"
    )
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("VEKTRA_")},
        "VEKTRA_ASSESS_ENABLED": "true",
        "VEKTRA_ASSESS_SECRETS_DIR": str(tmp_path / "absent"),
        **extra,
    }
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.stdout.strip() == "ceiling ok", out.stderr[-600:]
