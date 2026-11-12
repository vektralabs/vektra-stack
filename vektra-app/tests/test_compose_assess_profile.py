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
#: the credentials the overlay's LMS provider declares (``required_secrets``), which the matrix
#: does not list (ADR-0033 D5, plan 2a-4b.13); the provider's own suite checks the overlay mounts
#: exactly these for the worker (vektra-moodle-discuss provider/tests/test_entry_point.py)
PROVIDER_SECRETS = {"assess_ws_tokens"}


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
    if role == "worker":  # the worker alone holds the provider's credentials
        allowed |= PROVIDER_SECRETS
    required = {n for n, s in KEY_MATRIX.items() if s.need(role) is Need.REQUIRED}  # type: ignore[arg-type]
    assert mounted <= allowed, (
        f"{service} mounts forbidden secrets: {sorted(mounted - allowed)}"
    )
    assert required <= mounted, (
        f"{service} misses required secrets: {sorted(required - mounted)}"
    )


def test_the_provider_secrets_are_mounted_by_the_worker_alone(rendered: dict) -> None:  # type: ignore[type-arg]
    holders = {
        name
        for name, svc in rendered["services"].items()
        if _secrets(svc) & PROVIDER_SECRETS
    }
    assert holders == {"vektra-assess-worker"}
    assert PROVIDER_SECRETS <= _secrets(rendered["services"]["vektra-assess-worker"])
    assert not PROVIDER_SECRETS & set(KEY_MATRIX)


def test_every_assess_service_runs_the_image_with_the_provider(rendered: dict) -> None:  # type: ignore[type-arg]
    """ADR-0033 D6, OD-6: the platform image holds no LMS provider, so the overlay builds the
    variant with the Moodle one and every service that runs Vektra code runs it; the base file
    alone still builds the platform image."""
    services = rendered["services"]
    assert services["vektra"]["build"]["args"]["INSTALL_LMS_PROVIDER"] == "moodle"
    for name in ("vektra", "vektra-assess-worker", "vektra-assess-parser"):
        assert services[name]["image"] == "vektra-stack-lms-moodle", name
    base = _render("docker-compose.yml", profile=None)["services"]["vektra"]
    assert base["image"] == "vektra-stack"
    assert "INSTALL_LMS_PROVIDER" not in base["build"].get("args", {})


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
    # its own healthcheck: the image one probes the API port it cannot reach
    assert "vektra_assess.parser_sidecar.ping" in " ".join(p["healthcheck"]["test"])
    assert "--deep" in p["healthcheck"]["test"]


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


def test_worker_heartbeat_file_is_on_tmpfs_and_the_healthcheck_reads_it(
    rendered: dict,
) -> None:  # type: ignore[type-arg]
    w = rendered["services"]["vektra-assess-worker"]
    path = w["environment"]["VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE"]
    assert path == "/run/assess-worker/heartbeat"
    tmpfs = [
        t["target"] if isinstance(t, dict) else str(t).split(":")[0] for t in w["tmpfs"]
    ]
    assert "/run/assess-worker" in tmpfs
    probe = " ".join(w["healthcheck"]["test"])
    assert "VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE" in probe and "< 30" in probe
    assert "env_file" not in w, (
        "the worker takes its DSN and settings from environment only"
    )


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
        "log_min_error_statement=panic",
    ):
        assert flag in cmd


def test_parser_service_hardening_is_complete(rendered: dict) -> None:  # type: ignore[type-arg]
    p = rendered["services"]["vektra-assess-parser"]
    assert p["network_mode"] == "none" and p["read_only"] is True
    assert p["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in p["security_opt"]
    assert p["environment"]["CMD_TARGET"] == "assess-parser"
    assert "env_file" not in p, "the parser must not read a shared .env"
    assert "secrets" not in p and "ports" not in p
    assert int(p["pids_limit"]) == 64
    assert int(p["mem_limit"]) == int(p["memswap_limit"]) == 768 * 1024 * 1024
    assert float(p["cpus"]) == 1.5
    # `config --format json` renders zero soft/hard limits as {}; the container test reads the
    # effective value from `docker inspect`
    assert "core" in p["ulimits"]
    assert p["tmpfs"] == ["/tmp:size=64m,mode=1777"]
    assert p["restart"] == "unless-stopped"
    sock = [v for v in p["volumes"] if v.get("source") == SOCKET_VOLUME]
    assert [v["target"] for v in sock] == ["/run/vektra-assess-parser"]
    env = p["environment"]
    assert not [
        k
        for k in env
        if any(
            f in k
            for f in ("DATABASE_URL", "DSN", "PASSWORD", "SECRET", "TOKEN", "KEY")
        )
    ]


def test_socket_volume_consumers_are_exactly_parser_and_worker(rendered: dict) -> None:  # type: ignore[type-arg]
    consumers = {
        n
        for n, s in rendered["services"].items()
        if any(v.get("source") == SOCKET_VOLUME for v in s.get("volumes", []))
    }
    assert consumers == {"vektra-assess-parser", "vektra-assess-worker"}
    # and no service reaches it through volumes_from or a bind of the same path
    for n, s in rendered["services"].items():
        assert "volumes_from" not in s, n
        if n not in consumers:
            assert not [
                v
                for v in s.get("volumes", [])
                if "vektra-assess-parser" in str(v.get("target"))
            ], n


def test_the_shipped_overlay_keeps_the_worker_fail_closed(rendered: dict) -> None:  # type: ignore[type-arg]
    """Review finding: VEKTRA_ENV defaults to development, so the override must not ship by default."""
    env = rendered["services"]["vektra-assess-worker"]["environment"]
    assert "VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND" not in env


def test_the_worker_gets_the_discussion_model_settings_and_nothing_of_the_rag(
    rendered: dict,  # type: ignore[type-arg]
) -> None:
    """M1.5a: the worker runs Part B, so it needs the assess model, base, extra body and the
    shared key (environment only, no env_file); the RAG's extra body and fallback never."""
    env = rendered["services"]["vektra-assess-worker"]["environment"]
    for key in (
        "VEKTRA_ASSESS_LLM_MODEL",
        "VEKTRA_ASSESS_LLM_API_BASE",
        "VEKTRA_LLM_API_KEY",
        "VEKTRA_ASSESS_LLM_EXTRA_BODY",
    ):
        assert key in env, key
    for key in (
        "VEKTRA_LLM_EXTRA_BODY",
        "VEKTRA_LLM_FALLBACK_MODEL",
        "VEKTRA_LLM_PROVIDER",
    ):
        assert key not in env, key


def test_the_worker_gets_the_lifecycle_settings_the_api_reads(
    rendered: dict,  # type: ignore[type-arg]
) -> None:
    """CodeRabbit 4493109287: the pre-flight must use the same T, admission K, render freshness
    and model parameters as the API's Start (the worker has no env_file)."""
    env = rendered["services"]["vektra-assess-worker"]["environment"]
    for key in (
        "VEKTRA_ASSESS_START_T_S",
        "VEKTRA_ASSESS_ADMISSION_K",
        "VEKTRA_ASSESS_RENDER_FRESH_S",
        "VEKTRA_ASSESS_LLM_TEMPERATURE",
        "VEKTRA_ASSESS_LLM_MAX_TOKENS",
        "VEKTRA_ASSESS_LLM_CONTEXT_WINDOW",
        "VEKTRA_ASSESS_OUTAGE_TIMEOUT_S",
        # plan 10 (CodeRabbit 4503025939): transcript times and the file-name date
        "VEKTRA_ASSESS_SITE_TIMEZONE",
    ):
        assert env.get(key), key


def test_the_dev_file_only_opts_the_worker_in_and_pins_the_environment() -> None:
    dev = _render(
        "docker-compose.yml",
        "docker-compose.assess.yml",
        "docker-compose.assess.dev.yml",
    )
    env = dev["services"]["vektra-assess-worker"]["environment"]
    assert env["VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND"] == "true"
    assert env["VEKTRA_ENV"] == "development", "explicit, not the code default"
    base = _render("docker-compose.yml", "docker-compose.assess.yml")
    # the API gets only the explicit dev settings of the discussion runner (plan assess-llm-runner):
    # no activated model and no Presidio in dev/CI; the bypass is refused outside development
    api_dev = {
        "VEKTRA_ENV": "development",
        "VEKTRA_ASSESS_SAFEGUARD_MODE": "none",
        "VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED": "true",
    }
    api_env = dev["services"]["vektra"]["environment"]
    for key, value in api_dev.items():
        assert api_env[key] == value, key
    assert api_env["VEKTRA_ASSESS_LLM_MODEL"]
    for name, svc in dev["services"].items():
        if name == "vektra-assess-worker":
            continue
        if name == "vektra":
            svc = dict(svc)
            svc["environment"] = _without_permitted_overrides(
                svc["environment"],
                base["services"][name]["environment"],
                {*api_dev, "VEKTRA_ASSESS_LLM_MODEL"},
            )
        assert svc == base["services"][name], f"the dev file must not change {name}"


def test_every_assess_vektra_process_receives_vektra_env() -> None:
    """Review of #203: the worker and the parser have no env_file, so without an explicit
    entry they never see the operator's VEKTRA_ENV, and the API and the worker would judge
    "development" differently. Every assess service that runs the vektra image gets it, by
    interpolation like the other shared settings."""
    import yaml

    raw = yaml.safe_load((ROOT / "docker-compose.assess.yml").read_text())
    missing = [
        name
        for name, svc in raw["services"].items()
        # the API (``vektra``) reads VEKTRA_ENV from the base file's env_file; the overlay only
        # names its image variant
        if str(svc.get("image", "")).startswith("vektra-stack")
        and name != "vektra"
        and "VEKTRA_ENV" not in (svc.get("environment") or {})
    ]
    assert missing == []
    assert raw["services"]["vektra-assess-worker"]["environment"]["VEKTRA_ENV"] == (
        "${VEKTRA_ENV:-}"
    )


def test_the_dev_file_passes_the_safeguard_locality_rule() -> None:
    """Plan 2a-1.11: the dev overlay sets SAFEGUARD_MODE=none, which the API accepts only because
    the same file pins VEKTRA_ENV=development. The rendered API environment must pass the rule."""
    from vektra_app.assess_llm import check_safeguard_locality
    from vektra_assess.config import VektraAssessSettings

    dev = _render(
        "docker-compose.yml",
        "docker-compose.assess.yml",
        "docker-compose.assess.dev.yml",
    )
    api_env = dev["services"]["vektra"]["environment"]
    keys = (
        "VEKTRA_ENV",
        "VEKTRA_ASSESS_SAFEGUARD_MODE",
        "VEKTRA_ASSESS_PROVIDER_LOCALITY",
    )
    settings = VektraAssessSettings.model_validate(
        {k: api_env[k] for k in keys if api_env.get(k)}
    )
    assert settings.safeguard_mode == "none" and settings.env == "development"
    check_safeguard_locality(settings)  # accepted: no StartupValidationError


def _without_permitted_overrides(
    dev_env: dict[str, str], base_env: dict[str, str], permitted: set[str]
) -> dict[str, str]:
    """``dev_env`` with every permitted override undone: back to the base value when the base
    sets the key, dropped when it does not. Any other key is kept as is, so a change outside the
    permitted set still fails the comparison with the base."""
    out = {k: v for k, v in dev_env.items() if k not in permitted}
    out.update({k: base_env[k] for k in permitted if k in base_env})
    return out


def test_permitted_overrides_are_undone_to_the_base_value() -> None:
    base = {"VEKTRA_ENV": "production", "KEEP": "1"}
    dev = {"VEKTRA_ENV": "development", "KEEP": "1", "VEKTRA_ASSESS_LLM_MODEL": "m"}
    permitted = {"VEKTRA_ENV", "VEKTRA_ASSESS_LLM_MODEL"}
    # overridden base key restored, added key dropped
    assert _without_permitted_overrides(dev, base, permitted) == base
    # a non-permitted change survives and is caught by the comparison
    changed = {**dev, "KEEP": "2"}
    assert _without_permitted_overrides(changed, base, permitted) != base


def test_the_worker_gets_every_smtp_setting(
    rendered: dict,  # type: ignore[type-arg]
) -> None:
    """Plan 10b (CodeRabbit 4504362548): every relay setting the worker reads is forwarded,
    the CA file of an on-prem relay included; the account is a secret, never a variable."""
    from vektra_assess.config import VektraAssessSettings

    worker = rendered["services"]["vektra-assess-worker"]["environment"]
    smtp = {
        f.alias
        for name, f in VektraAssessSettings.model_fields.items()
        if f.alias
        and (
            name.startswith("smtp_")
            or name in ("message_id_domain", "recipient_domains")
        )
    }
    assert "VEKTRA_ASSESS_SMTP_CA_FILE" in smtp
    missing = sorted(k for k in smtp if k not in worker)
    assert missing == [], missing
    assert not any("USERNAME" in k or "PASSWORD" in k for k in worker)


def test_the_api_sees_the_relay_and_the_operator_address(  # type: ignore[type-arg]
    rendered: dict,
) -> None:
    """CodeRabbit 4524508939: settings exported in the shell reached the worker only, so the
    API refused the operator's SMTP test send (503) although the worker could send it."""
    env = rendered["services"]["vektra"].get("environment", {})
    for name in (
        "VEKTRA_ASSESS_SMTP_HOST",
        "VEKTRA_ASSESS_SMTP_FROM",
        "VEKTRA_ASSESS_OPERATOR_EMAIL",
    ):
        assert name in env, name
    # and the API gets the SAME interpolated value as the worker
    worker = rendered["services"]["vektra-assess-worker"].get("environment", {})
    for name in (
        "VEKTRA_ASSESS_SMTP_HOST",
        "VEKTRA_ASSESS_SMTP_FROM",
        "VEKTRA_ASSESS_OPERATOR_EMAIL",
    ):
        assert env[name] == worker.get(name), name


# --- every documented combination is a valid project, with and without the profile ------------
# CI builds the image variant with ``-f docker-compose.yml -f docker-compose.assess.yml build
# vektra`` and no --profile (integration.yml, assess-stack); an operator may do the same. The API's
# dependency on the profile-gated role provisioning must not make that project invalid.
COMBINATIONS = [
    ("docker-compose.yml",),
    ("docker-compose.yml", "docker-compose.assess.yml"),
    (
        "docker-compose.yml",
        "docker-compose.assess.yml",
        "docker-compose.assess.dev.yml",
    ),
    (
        "docker-compose.yml",
        "docker-compose.assess.yml",
        "docker-compose.assess.dev.yml",
        ".github/ci/compose.hf-cache.yml",
    ),
]


@pytest.mark.parametrize("profile", [None, "assess"])
@pytest.mark.parametrize("files", COMBINATIONS, ids=lambda f: "+".join(f))
def test_every_documented_combination_is_a_valid_project(
    files: tuple[str, ...], profile: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_CACHE_DIR", "/tmp/hf-cache")  # required by the CI-only file
    assert "vektra" in _render(*files, profile=profile)["services"]


def test_with_the_profile_the_api_still_waits_for_the_role_provisioning() -> None:
    """``required: false`` only lets the dependency be absent (no profile); when the profile
    brings the one-shot provisioning in, the API still starts after it completed."""
    rendered = _render("docker-compose.yml", "docker-compose.assess.yml")
    dep = rendered["services"]["vektra"]["depends_on"]["vektra-assess-db-init"]
    assert dep["condition"] == "service_completed_successfully"
    worker = rendered["services"]["vektra-assess-worker"]["depends_on"]
    assert (
        worker["vektra-assess-db-init"]["condition"] == "service_completed_successfully"
    )
