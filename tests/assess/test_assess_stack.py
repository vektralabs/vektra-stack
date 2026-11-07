"""Assess compose stack, observed from inside the containers (plan assess-foundations 7.6, 9.4).

Runs against `docker compose -f docker-compose.yml -f docker-compose.assess.yml
-f docker-compose.assess.dev.yml --profile
assess up`. Every claim is checked empirically: the parser's network is probed by trying
to connect, the roles by asking Postgres, the worker's memory by reading /proc.

The `assess-stack` CI job sets VEKTRA_ASSESS_STACK=1: then a missing stack is a FAILURE,
never a skip. Without it (a developer run with no stack) the module skips with a reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# The directory holding the compose files, .env and ./secrets/assess. Defaults to the
# repository; a local run can point it at an isolated copy (own .env, own project name
# via COMPOSE_PROJECT_NAME) so it never touches a developer's own stack or volumes.
ROOT = Path(
    os.environ.get("VEKTRA_ASSESS_COMPOSE_DIR", Path(__file__).resolve().parents[2])
)
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-f",
    str(ROOT / "docker-compose.assess.yml"),
    "-f",
    str(ROOT / "docker-compose.assess.dev.yml"),
    "--profile",
    "assess",
]
API = os.environ.get("VEKTRA_API_URL", "http://localhost:8000")
RSS_CEILING_MB = 300


def _compose(
    *args: str, check: bool = False, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _exec(service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    return _compose("exec", "-T", service, *cmd)


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    res = _compose("ps", "--status", "running", "--format", "json")
    running = set()
    for line in res.stdout.splitlines():
        if line.strip():
            running.add(json.loads(line)["Service"])
    needed = {"vektra", "vektra-assess-worker", "vektra-assess-parser", "postgres"}
    if not needed <= running:
        msg = f"assess stack not running (missing: {sorted(needed - running)})"
        if os.environ.get("VEKTRA_ASSESS_STACK") == "1":
            pytest.fail(f"BLOCKED: {msg}")
        pytest.skip(msg)


# --- 7.6 parser isolation ------------------------------------------------------

PROBE = r"""
import socket, os, json
out = {}
try:
    socket.create_connection(("postgres", 5432), timeout=3).close(); out["tcp_postgres"] = "connected"
except OSError as e:
    out["tcp_postgres"] = type(e).__name__
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close(); out["tcp_ip"] = "connected"
except OSError as e:
    out["tcp_ip"] = type(e).__name__
try:
    socket.getaddrinfo("postgres", 5432); out["dns"] = "resolved"
except OSError as e:
    out["dns"] = type(e).__name__
out["secrets_dir_exists"] = os.path.exists("/run/secrets")
out["socket_exists"] = os.path.exists(os.environ.get("VEKTRA_ASSESS_PARSER_SOCKET", "/nonexistent"))
print(json.dumps(out))
"""


def test_parser_has_no_network_and_no_secrets() -> None:
    res = _exec("vektra-assess-parser", "python", "-c", PROBE)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["tcp_postgres"] != "connected", out
    assert out["tcp_ip"] != "connected", out
    assert out["dns"] != "resolved", out
    assert out["secrets_dir_exists"] is False, out
    assert out["socket_exists"] is True, out  # it is serving


def test_api_container_cannot_see_the_parser_socket() -> None:
    res = _exec(
        "vektra",
        "sh",
        "-c",
        "test -e /run/vektra-assess-parser/parser.sock && echo present || echo absent",
    )
    assert res.stdout.strip() == "absent", res.stdout + res.stderr


def test_worker_reaches_the_parser_over_the_socket() -> None:
    code = (
        "import asyncio\n"
        "from vektra_assess.config import VektraAssessSettings\n"
        "from vektra_assess.worker.parser_client import ParserClient\n"
        "async def m():\n"
        "    c = ParserClient.from_settings(VektraAssessSettings())\n"
        "    r = await c.parse_file(b'hello from the worker', 'txt')\n"
        "    print(r.status.value, r.text, round(await c.ping(deep=True), 3) >= 0)\n"
        "asyncio.run(m())\n"
    )
    res = _exec("vektra-assess-worker", "python", "-c", code)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split(None, 1)[0] == "READ", res.stdout
    assert "hello from the worker" in res.stdout and res.stdout.strip().endswith("True")


# --- roles (INT-002) -----------------------------------------------------------

ROLE_PROBE = r"""
import asyncio, os, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from vektra_assess.db.roles import dsn_with_password
dsn, pwfile = sys.argv[1], sys.argv[2]
url = dsn_with_password(os.environ[dsn], open(pwfile).read().strip(), step="probe")
async def m():
    e = create_async_engine(url)
    async with e.connect() as c:
        row = (await c.execute(text("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"))).one()
    await e.dispose()
    print(row[0], row[1])
asyncio.run(m())
"""


@pytest.mark.parametrize(
    ("service", "dsn", "pwfile", "role"),
    [
        (
            "vektra",
            "VEKTRA_ASSESS_API_DATABASE_URL",
            "/run/secrets/assess_db_api_password",
            "vektra_assess_api",
        ),
        (
            "vektra-assess-worker",
            "VEKTRA_ASSESS_WORKER_DATABASE_URL",
            "/run/secrets/assess_db_worker_password",
            "vektra_assess_worker",
        ),
    ],
)
def test_assess_dsn_session_role(
    service: str, dsn: str, pwfile: str, role: str
) -> None:
    res = _exec(service, "python", "-c", ROLE_PROBE, dsn, pwfile)
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == [role, "False"]


def test_postgres_logs_no_statements() -> None:
    for setting, expected in (
        ("log_statement", "none"),
        ("log_parameter_max_length", "0"),
    ):
        res = _exec(
            "postgres",
            "psql",
            "-U",
            "vektra",
            "-d",
            "vektra",
            "-tAc",
            f"SHOW {setting}",
        )
        assert res.stdout.strip() == expected, (setting, res.stdout, res.stderr)


def test_api_startup_ran_assess_check_ok() -> None:
    logs = _compose("logs", "vektra").stdout.splitlines()
    ok = [ln for ln in logs if "assess_check" in ln and re.search(r"status\W+ok", ln)]
    assert ok, "no 'assess_check ... ok' line in the API startup log"


# --- 9.4 worker idle RSS and import ceiling ----------------------------------------


def test_worker_idle_rss_and_heartbeat() -> None:
    hb = _exec(
        "vektra-assess-worker",
        "python",
        "-c",
        # the path comes from the container's own environment (the overlay puts it on tmpfs)
        "import os,time; p=os.environ['VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE']; "
        "print(time.time()-os.path.getmtime(p))",
    )
    assert hb.returncode == 0, f"worker never reached the heartbeat state: {hb.stderr}"
    assert float(hb.stdout) < 30
    time.sleep(15)  # idle, as the plan measures it
    status = _exec("vektra-assess-worker", "cat", "/proc/1/status").stdout
    rss_kb = int(
        next(
            line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")
        )
    )
    print(f"assess worker idle VmRSS: {rss_kb / 1024:.1f} MB")
    assert rss_kb / 1024 <= RSS_CEILING_MB, (
        f"VmRSS {rss_kb / 1024:.1f} MB > {RSS_CEILING_MB} MB"
    )


def test_worker_loaded_no_forbidden_module() -> None:
    code = (
        "import sys, vektra_app.assess_worker as w\n"
        "from vektra_core.providers.litellm_provider import LitellmProvider\n"
        "import vektra_app.assess_llm\n"
        "print([m for m in w.FORBIDDEN_MODULES if m in sys.modules])\n"
    )
    res = _exec("vektra-assess-worker", "python", "-c", code)
    assert res.stdout.strip() == "[]", res.stdout + res.stderr


# --- health: a stopped worker does not take the API down (COMP-002) --------------


def _get(path: str, token: str | None = None) -> tuple[int, dict]:  # type: ignore[type-arg]
    req = urllib.request.Request(f"{API}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture(scope="module")
def admin_key() -> str:
    """An admin key minted with the bootstrap key (single use) of the CI stack."""
    if key := os.environ.get("VEKTRA_ASSESS_ADMIN_KEY"):
        return key
    bootstrap = os.environ.get("VEKTRA_BOOTSTRAP_KEY")
    if not bootstrap:
        pytest.fail("BLOCKED: set VEKTRA_BOOTSTRAP_KEY or VEKTRA_ASSESS_ADMIN_KEY")
    req = urllib.request.Request(
        f"{API}/api/v1/api-keys",
        data=json.dumps({"label": "assess-stack-test", "scopes": ["admin"]}).encode(),
        headers={
            "Authorization": f"Bearer {bootstrap}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        key: str = json.loads(r.read())["key"]
    return key


def test_stopped_worker_leaves_shallow_health_healthy(admin_key: str) -> None:
    _compose("stop", "vektra-assess-worker", check=True)
    try:
        code, body = _get("/health")
        assert code == 200 and body["status"] == "healthy", body
        # Acceptance (plan assess-job-loop): not-ready within 65 s of the stop. The worker writes
        # state "stopped" into its heartbeat row on SIGTERM, so readiness drops at once; the row
        # aging past VEKTRA_ASSESS_HEARTBEAT_MAX_AGE_S (60 s) is only the fallback for a kill -9.
        # A 15 s deadline would not be enough to tell the two apart by time alone, so the row's
        # own state is asserted below: a lost "stopped" write must fail this test.
        deadline = time.time() + 15
        while True:
            code, body = _get("/health/assess", admin_key)
            assert code == 200, body
            if body["status"] not in ("healthy", "disabled") or time.time() > deadline:
                break
            time.sleep(2)
        assert body["status"] not in ("healthy", "disabled"), (
            f"/health/assess still {body} 15 s after docker stop"
        )
        state = _exec(
            "postgres",
            "psql",
            "-U",
            "vektra",
            "-d",
            "vektra",
            "-tAc",
            "SELECT details->>'state' FROM assess_heartbeat WHERE component = 'worker'",
        )
        assert state.stdout.strip() == "stopped", (
            f"the worker did not write 'stopped' on SIGTERM: {state.stdout!r} {state.stderr!r}"
        )
    finally:
        _compose("start", "vektra-assess-worker", check=True)
