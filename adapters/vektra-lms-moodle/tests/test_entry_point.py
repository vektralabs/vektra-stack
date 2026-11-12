"""The provider as a distribution (ADR-0033 D2, D5, D6; plan 2a-4b): how the platform finds it,
what it declares, what it reads, and what it must not lose by leaving ``vektra_assess``.

- the entry point ``vektra.lms_providers: moodle`` is the installed distribution's, and the
  worker's own loader builds it;
- the secret it declares is the one the assess compose overlay mounts for the worker alone;
- its loggers keep the strict redaction (importing the package registers the prefix) and the
  vertical's log guards (named loggers, literal messages) hold on its source too;
- its settings are ``VEKTRA_LMS_MOODLE_*`` (OD-4, no alias), each documented;
- the plain-http switch still stops the worker outside development (moved from vektra-app).
"""

from __future__ import annotations

import asyncio
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import structlog

import vektra_lms_moodle
from vektra_lms_moodle.config import MoodleSettings
from vektra_lms_moodle.provider import MoodleLmsProvider, build_provider
from vektra_shared.startup import StartupValidationError

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(vektra_lms_moodle.__file__).resolve().parent
CANARY = "CNRY-ws_token-0123456789abcdef"


def test_the_entry_point_is_this_distribution_and_loads() -> None:
    (ep,) = [
        e for e in entry_points(group="vektra.lms_providers") if e.name == "moodle"
    ]
    assert ep.dist is not None and ep.dist.name == "vektra-lms-moodle"
    assert ep.value == "vektra_lms_moodle.provider:build_provider"
    assert ep.load() is build_provider

    from vektra_app.lms_providers import available_lms_providers, load_lms_provider

    assert "moodle" in available_lms_providers()
    provider = asyncio.run(load_lms_provider("moodle", development=False))
    try:
        assert isinstance(provider, MoodleLmsProvider) and provider.name == "moodle"
    finally:
        asyncio.run(provider.aclose())


def test_the_declared_secret_is_the_one_the_overlay_mounts_for_the_worker() -> None:
    import yaml

    provider = build_provider(development=False)
    try:
        declared = set(provider.required_secrets)
    finally:
        asyncio.run(provider.aclose())
    assert declared == {"assess_ws_tokens"}
    compose = yaml.safe_load((ROOT / "docker-compose.assess.yml").read_text())
    mounts = {
        name: {s["source"] for s in svc.get("secrets", [])}
        for name, svc in compose["services"].items()
    }
    assert declared <= mounts["vektra-assess-worker"]
    assert [n for n, m in mounts.items() if m & declared] == ["vektra-assess-worker"]
    # and the platform's compose test names the same set (vektra-app/tests)
    app_test = (ROOT / "vektra-app/tests/test_compose_assess_profile.py").read_text()
    assert 'PROVIDER_SECRETS = {"assess_ws_tokens"}' in app_test


def test_the_worker_loads_the_declared_secret_through_the_key_matrix(
    tmp_path: Path,
) -> None:
    """Plan 2a-4b.13 end to end: the matrix loads what the provider declares, by its name."""
    from vektra_assess.keys import (
        KEY_MATRIX,
        Credentials,
        Need,
        enforce_key_matrix,
        generate,
    )

    provider = build_provider(development=False)
    asyncio.run(provider.aclose())
    full = tmp_path / "all"
    generate(full, provider_secrets=provider.required_secrets)
    d = tmp_path / "worker"
    d.mkdir()
    names = {n for n, s in KEY_MATRIX.items() if s.worker is Need.REQUIRED}
    for name in names | set(provider.required_secrets):
        (d / name).write_bytes((full / name).read_bytes())
        (d / name).chmod(0o400)
    loaded = enforce_key_matrix(
        "worker", secrets_dir=d, environ={}, provider_secrets=provider.required_secrets
    )
    assert isinstance(loaded["assess_ws_tokens"], Credentials)
    (d / "assess_ws_tokens").unlink()
    with pytest.raises(StartupValidationError, match="assess_ws_tokens: required"):
        enforce_key_matrix(
            "worker",
            secrets_dir=d,
            environ={},
            provider_secrets=provider.required_secrets,
        )


def test_the_provider_logs_are_redacted(canary_log_capture: Any) -> None:
    from vektra_lms_moodle import client

    # no registration here: importing the package registered the prefix
    client.log.warning(
        "assess_probe", wsfunction="core_webservice_get_site_info", body=CANARY
    )
    structlog.get_logger("vektra_lms_moodle.readers").info("assess_read", token=CANARY)
    canary_log_capture.assert_clean([CANARY])
    assert len(canary_log_capture.lines()) == 2


def test_the_vertical_log_guards_hold_on_the_provider_source() -> None:
    """The guards of ``vektra-assess/tests/test_log_safety_wiring.py`` covered this code while it
    was ``vektra_assess.moodle``; they run on it here, unchanged."""
    import test_log_safety_wiring as guards

    assert list(SRC.rglob("*.py")), "no source found"
    assert guards._violations(SRC) == []
    assert guards._message_violations(SRC) == []


# --- settings (OD-4) ---------------------------------------------------------------------------


def _aliases() -> set[str]:
    return {f.alias for f in MoodleSettings.model_fields.values() if f.alias}


def test_every_setting_has_the_provider_prefix_or_is_shared() -> None:
    shared = {"VEKTRA_ASSESS_SECRETS_DIR"}  # development is passed in, never read here
    own = _aliases() - shared
    assert shared <= _aliases()
    assert own and all(a.startswith("VEKTRA_LMS_MOODLE_") for a in own), own


@pytest.mark.parametrize("doc", ["docs/reference/configuration.md", ".env.example"])
def test_every_setting_is_documented_and_nothing_else(doc: str) -> None:
    text = (ROOT / doc).read_text()
    own = {a for a in _aliases() if a.startswith("VEKTRA_LMS_MOODLE_")}
    missing = sorted(a for a in own if not re.search(rf"\b{a}\b", text))
    assert missing == []
    documented = set(re.findall(r"\bVEKTRA_LMS_MOODLE_[A-Z0-9_]*[A-Z0-9]\b", text))
    assert documented - own == set()


def test_the_old_names_are_not_read() -> None:
    """No alias (OD-4): a value under an old name is ignored, never applied."""
    s = MoodleSettings(
        **{"VEKTRA_ASSESS_WS_RPS": "999", "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP": "true"}
    )  # type: ignore[arg-type]
    assert s.ws_rps == 10.0 and s.moodle_allow_http is False
    env_example = (ROOT / ".env.example").read_text()
    old = re.findall(
        r"VEKTRA_ASSESS_(?:MOODLE_\w+|WS_\w+|BREAKER_\w+|INGEST_ACCOUNT_\w+)",
        env_example,
    )
    assert old == []


# --- the development-only switch stops the worker (moved from vektra-app/tests) ---------------


@pytest.mark.parametrize("env", ["production", None])
def test_the_plain_http_switch_is_refused_by_the_provider(
    monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    from vektra_app import assess_worker
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_LMS_PROVIDER", "moodle")
    monkeypatch.setenv("VEKTRA_LMS_MOODLE_ALLOW_HTTP", "true")
    if env is None:
        monkeypatch.delenv("VEKTRA_ENV", raising=False)
    else:
        monkeypatch.setenv("VEKTRA_ENV", env)
    with pytest.raises(StartupValidationError, match="ALLOW_HTTP"):
        asyncio.run(assess_worker._load_lms(VektraAssessSettings()))
    monkeypatch.setenv("VEKTRA_ENV", "development")
    provider = asyncio.run(assess_worker._load_lms(VektraAssessSettings()))
    try:
        assert provider.name == "moodle"
    finally:
        asyncio.run(provider.aclose())


# --- review of #207 ----------------------------------------------------------------------------


def test_the_secret_names_are_declared_on_the_entry_point() -> None:
    """Finding 1: readable without building the provider, and the same as the Protocol's."""
    from vektra_app.lms_providers import installed_provider_secrets

    assert build_provider.REQUIRED_SECRETS == ("assess_ws_tokens",)
    assert "assess_ws_tokens" in installed_provider_secrets()
    provider = build_provider(development=False)
    try:
        assert provider.required_secrets == build_provider.REQUIRED_SECRETS
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.parametrize(
    "carrier", ["ASSESS_WS_TOKENS", "VEKTRA_ASSESS_WS_TOKENS_FILE"]
)
@pytest.mark.parametrize("enabled", [False, True])
async def test_the_api_refuses_an_env_var_carrying_the_ws_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, carrier: str, enabled: bool
) -> None:
    """Finding 1 (ARCH-066), through the API's own startup step: with the Moodle provider
    installed, the API refuses a variable carrying its token secret, assess enabled or not."""
    from types import SimpleNamespace

    from vektra_app import assess_wiring
    from vektra_assess.keys import KEY_MATRIX, Need, generate

    d = tmp_path / "secrets"
    if enabled:
        full = tmp_path / "all"
        generate(full)
        d.mkdir()
        for name, spec in KEY_MATRIX.items():
            if spec.api is Need.REQUIRED:
                (d / name).write_bytes((full / name).read_bytes())
                (d / name).chmod(0o400)
        monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
        monkeypatch.setenv("VEKTRA_ENV", "development")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(d))
    monkeypatch.setenv(carrier, "a-ws-token")
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(StartupValidationError) as e:
        await assess_wiring.step_12_assess_check(app, "postgresql+asyncpg://u@h/db")  # type: ignore[arg-type]
    assert (
        f"assess_ws_tokens: forbidden for api but carried by env var(s) ['{carrier}']"
        in e.value.detail
    )
    assert "a-ws-token" not in e.value.detail


def test_bare_field_names_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 2: only the ``VEKTRA_LMS_MOODLE_*`` names configure the provider, never a bare
    field name a container happens to carry."""
    for bare in (
        "ENV",
        "MOODLE_ALLOW_HTTP",
        "WS_RPS",
        "SECRETS_DIR",
        "moodle_allow_http",
    ):
        monkeypatch.setenv(bare, "development" if bare == "ENV" else "true")
    monkeypatch.setenv("ws_rps", "999")
    s = MoodleSettings()
    assert s.moodle_allow_http is False and s.ws_rps == 10.0
    assert str(s.secrets_dir) == "/run/secrets"
    assert MoodleSettings.model_config.get("populate_by_name") is False


def test_a_stray_env_does_not_make_the_provider_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Findings 2 and 8: the provider has no development switch of its own any more; only the
    flag the composition root passes counts."""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("VEKTRA_ENV", "development")
    monkeypatch.setenv("VEKTRA_LMS_MOODLE_ALLOW_HTTP", "true")
    assert not hasattr(MoodleSettings(), "is_development")
    provider = build_provider(development=False)
    try:
        assert provider.client.plain_http_allowed is False
        (problem,) = provider.startup_problems(False)
        assert problem.setting == "VEKTRA_LMS_MOODLE_ALLOW_HTTP"
    finally:
        asyncio.run(provider.aclose())
    provider = build_provider(development=True)
    try:
        assert provider.client.plain_http_allowed is True
        assert provider.startup_problems(True) == ()
    finally:
        asyncio.run(provider.aclose())


def test_the_call_deadline_is_the_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 3: the Protocol's ``call_deadline_s`` is the client's total call deadline, so
    raising the provider's timeout raises the vertical's budget."""
    from vektra_assess.jobs import JobKind
    from vektra_assess.worker.policy import load_policy

    provider = build_provider(development=False)
    asyncio.run(provider.aclose())
    assert provider.call_deadline_s == 30.0
    monkeypatch.setenv("VEKTRA_LMS_MOODLE_WS_TOTAL_TIMEOUT_S", "90")
    slow = build_provider(development=False)
    asyncio.run(slow.aclose())
    assert slow.call_deadline_s == 90.0
    budget = load_policy({}, lms_call_deadline_s=slow.call_deadline_s).kinds[
        JobKind.TEACHER_GATE
    ]
    assert budget.budget_s >= 3 * 90 + 5


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("VEKTRA_ASSESS_WS_RPS", "VEKTRA_LMS_MOODLE_WS_RPS"),
        ("VEKTRA_ASSESS_WS_TOTAL_TIMEOUT_S", "VEKTRA_LMS_MOODLE_WS_TOTAL_TIMEOUT_S"),
        ("VEKTRA_ASSESS_BREAKER_THRESHOLD", "VEKTRA_LMS_MOODLE_BREAKER_THRESHOLD"),
        ("VEKTRA_ASSESS_MOODLE_ALLOW_HTTP", "VEKTRA_LMS_MOODLE_ALLOW_HTTP"),
        ("VEKTRA_ASSESS_MOODLE_CA_FILE", "VEKTRA_LMS_MOODLE_CA_FILE"),
        (
            "VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME",
            "VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME",
        ),
    ],
)
def test_an_old_setting_name_refuses_the_start_naming_the_new_one(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    """Finding 4: a value under a pre-2a-4 name would otherwise be ignored in silence."""
    from vektra_app import assess_worker
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv(old, "1")
    provider = build_provider(development=False)
    try:
        problems = provider.startup_problems(False)
    finally:
        asyncio.run(provider.aclose())
    assert len(problems) == 1 and f"{old} -> {new}" in problems[0].detail
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_LMS_PROVIDER", "moodle")
    with pytest.raises(StartupValidationError, match=f"{old} -> {new}"):
        asyncio.run(assess_worker._load_lms(VektraAssessSettings()))


def test_every_old_name_set_at_once_is_listed_and_the_live_test_variables_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_WS_RPS", "1")
    monkeypatch.setenv("VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS", "10.0.0.0/8")
    for live in (
        "VEKTRA_ASSESS_MOODLE43_URL",
        "VEKTRA_ASSESS_MOODLE52_URL",
        "VEKTRA_ASSESS_MOODLE_MANIFEST_DIR",
        "VEKTRA_ASSESS_MOODLE_TOKEN_DIR",
    ):
        monkeypatch.setenv(live, "/x")
    provider = build_provider(development=False)
    try:
        (problem,) = provider.startup_problems(False)
    finally:
        asyncio.run(provider.aclose())
    assert "VEKTRA_ASSESS_WS_RPS -> VEKTRA_LMS_MOODLE_WS_RPS" in problem.detail
    assert (
        "VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS -> VEKTRA_LMS_MOODLE_ALLOWED_CIDRS"
        in problem.detail
    )
    assert "MOODLE43" not in problem.detail and "MANIFEST" not in problem.detail
    assert "10.0.0.0" not in problem.detail  # names only, never a value


def test_an_invalid_setting_is_explained_by_the_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 5: the variable and its rule, not "failed to load (ValidationError)"."""
    from vektra_app.lms_providers import load_lms_provider

    monkeypatch.setenv("VEKTRA_LMS_MOODLE_WS_RPS", "0")
    with pytest.raises(StartupValidationError) as e:
        asyncio.run(load_lms_provider("moodle", development=False))
    assert "VEKTRA_LMS_MOODLE_WS_RPS: Input should be greater than 0" in e.value.detail
    assert "ValidationError" not in e.value.detail
    assert "VEKTRA_LMS_MOODLE_WS_RPS" in e.value.remediation


def test_importing_the_package_registers_the_strict_prefix() -> None:
    """Finding 6: every construction path is covered, a client built directly included,
    because the package registers its prefix when it is imported."""
    import subprocess
    import sys

    code = (
        "import vektra_lms_moodle.client\n"
        "from vektra_shared.log_safety import is_strict_logger\n"
        "assert is_strict_logger('vektra_lms_moodle.client')\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_a_directly_built_client_logs_strictly(canary_log_capture: Any) -> None:
    from vektra_lms_moodle.client import MoodleWsClient, log

    client = MoodleWsClient(MoodleSettings(), token_provider=lambda _s: None)
    try:
        log.warning("assess_probe", body=CANARY)
    finally:
        asyncio.run(client.aclose())
    canary_log_capture.assert_clean([CANARY])
