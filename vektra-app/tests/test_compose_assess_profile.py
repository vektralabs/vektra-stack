"""Compose policy for the assess overlay (plan assess-foundations 7.5, ARCH-066/071/075).

Reads the RENDERED configuration (`docker compose ... config`), not the YAML source, so
anchors, interpolation and file merging are what get tested. The allowed secrets per
service come from ``KEY_MATRIX`` itself: the matrix and the compose file cannot diverge.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vektra_assess.keys import DB_INIT_SECRETS, KEY_MATRIX, Need

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROLE = {
    "vektra": "api",
    "vektra-assess-worker": "worker",
    "vektra-assess-parser": "parser",
}
SOCKET_VOLUME = "assess_parser_sock"


def _render(*files: str, profile: str | None = "assess") -> dict:  # type: ignore[type-arg]
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if (
        res.returncode != 0
        and "docker compose" in res.stderr
        and "not a docker command" in res.stderr
    ):
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr
    doc: dict = json.loads(res.stdout)  # type: ignore[type-arg]
    return doc


@pytest.fixture(scope="module")
def rendered() -> dict:  # type: ignore[type-arg]
    return _render("docker-compose.yml", "docker-compose.assess.yml")


def _secrets(svc: dict) -> set[str]:  # type: ignore[type-arg]
    return {s["source"] for s in svc.get("secrets", [])}


def _volumes(svc: dict) -> set[str]:  # type: ignore[type-arg]
    return {
        v.get("source") for v in svc.get("volumes", []) if v.get("type") == "volume"
    }


def test_all_assess_services_present(rendered: dict) -> None:  # type: ignore[type-arg]
    assert {
        "vektra",
        "vektra-assess-db-init",
        "vektra-assess-worker",
        "vektra-assess-parser",
    } <= set(rendered["services"])


@pytest.mark.parametrize(("service", "role"), sorted(SERVICE_ROLE.items()))
def test_service_secrets_match_key_matrix(
    rendered: dict, service: str, role: str
) -> None:  # type: ignore[type-arg]
    mounted = _secrets(rendered["services"][service])
    allowed = {n for n, s in KEY_MATRIX.items() if s.need(role) is not Need.FORBIDDEN}  # type: ignore[arg-type]
    required = {n for n, s in KEY_MATRIX.items() if s.need(role) is Need.REQUIRED}  # type: ignore[arg-type]
    assert mounted <= allowed, (
        f"{service} mounts forbidden secrets: {sorted(mounted - allowed)}"
    )
    assert required <= mounted, (
        f"{service} misses required secrets: {sorted(required - mounted)}"
    )


def test_db_init_holds_only_role_passwords(rendered: dict) -> None:  # type: ignore[type-arg]
    assert _secrets(rendered["services"]["vektra-assess-db-init"]) == set(
        DB_INIT_SECRETS
    )


def test_parser_is_isolated(rendered: dict) -> None:  # type: ignore[type-arg]
    p = rendered["services"]["vektra-assess-parser"]
    assert p.get("network_mode") == "none"
    assert _secrets(p) == set()
    assert p.get("read_only") is True
    assert p.get("user") == "1000:1000"
    assert "ports" not in p
    env = p.get("environment", {})
    assert not [k for k in env if "DATABASE_URL" in k or "PASSWORD" in k]
    assert p.get("mem_limit") and p.get("pids_limit")


def test_socket_volume_only_in_worker_and_parser(rendered: dict) -> None:  # type: ignore[type-arg]
    holders = {
        n for n, s in rendered["services"].items() if SOCKET_VOLUME in _volumes(s)
    }
    assert holders == {"vektra-assess-worker", "vektra-assess-parser"}


def test_worker_has_no_listener_and_bounded_stop(rendered: dict) -> None:  # type: ignore[type-arg]
    w = rendered["services"]["vektra-assess-worker"]
    assert "ports" not in w
    assert w["environment"]["CMD_TARGET"] == "assess-worker"
    assert w.get("stop_grace_period") in ("30s", "30000000000", 30000000000)
    assert "healthcheck" in w


def test_assess_dsns_carry_no_password(rendered: dict) -> None:  # type: ignore[type-arg]
    from sqlalchemy.engine import make_url

    for name, key in (
        ("vektra", "VEKTRA_ASSESS_API_DATABASE_URL"),
        ("vektra-assess-worker", "VEKTRA_ASSESS_WORKER_DATABASE_URL"),
    ):
        url = make_url(rendered["services"][name]["environment"][key])
        assert url.password is None, name


def test_base_stack_alone_has_no_assess_secret_or_service() -> None:
    """Without the overlay nothing assess is mounted or started (dormant by default)."""
    base = _render("docker-compose.yml", profile=None)
    assert not [n for n in base["services"] if "assess" in n]
    assert _secrets(base["services"]["vektra"]) == set()


def test_postgres_logs_no_statements(rendered: dict) -> None:  # type: ignore[type-arg]
    cmd = " ".join(rendered["services"]["postgres"]["command"])
    for flag in (
        "log_statement=none",
        "log_min_duration_statement=-1",
        "log_parameter_max_length=0",
        "log_parameter_max_length_on_error=0",
    ):
        assert flag in cmd
