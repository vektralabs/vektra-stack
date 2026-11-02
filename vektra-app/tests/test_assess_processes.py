"""Assess worker and parser entrypoints (plan assess-foundations 6.1b, 6.2, 6.3, 6.5).

The entrypoints are run as real subprocesses where the claim is about the process
(exit codes when disabled); the checks are driven into refusal with real files.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
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


async def test_heartbeat_refreshes_and_stops_promptly(tmp_path: Path) -> None:
    hb = tmp_path / "hb"
    stop = asyncio.Event()
    task = asyncio.create_task(assess_worker.heartbeat_loop(hb, stop, interval_s=0.05))
    await asyncio.sleep(0.02)
    first = hb.stat().st_mtime_ns
    await asyncio.sleep(0.15)
    assert hb.stat().st_mtime_ns > first
    t0 = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert time.monotonic() - t0 < 0.5


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


async def test_heartbeat_loop_touches_file_and_beats_the_database(tmp_path):
    import asyncio

    from vektra_app.assess_worker import heartbeat_loop

    calls = []

    class Conn:
        async def execute(self, sql):
            calls.append(str(sql))

    class Begin:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Engine:
        def begin(self):
            return Begin()

    stop = asyncio.Event()
    path = tmp_path / "hb"
    task = asyncio.create_task(
        heartbeat_loop(path, stop, interval_s=0.01, engine=Engine())
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert path.exists()
    assert calls and "assess_heartbeat" in calls[0]


async def test_a_failing_heartbeat_write_never_stops_the_file_heartbeat(tmp_path):
    import asyncio

    from vektra_app.assess_worker import heartbeat_loop

    class Engine:
        def begin(self):
            raise ConnectionError("db down")

    stop = asyncio.Event()
    path = tmp_path / "hb"
    task = asyncio.create_task(
        heartbeat_loop(path, stop, interval_s=0.01, engine=Engine())
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert path.exists()


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
