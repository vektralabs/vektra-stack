"""The provider as a distribution (ADR-0033 D2, D5, D6; plan 2a-4b): how the platform finds it,
what it declares, what it reads, and what it must not lose by leaving ``vektra_assess``.

- the entry point ``vektra.lms_providers: moodle`` is the installed distribution's, and the
  worker's own loader builds it;
- the secret it declares is the one the assess compose overlay mounts for the worker alone;
- its loggers keep the strict redaction (``build_provider`` registers the prefix) and the
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

    from vektra_app.lms_providers import available_lms_providers, load_lms_provider

    assert "moodle" in available_lms_providers()
    provider = asyncio.run(load_lms_provider("moodle"))
    try:
        assert isinstance(provider, MoodleLmsProvider) and provider.name == "moodle"
    finally:
        asyncio.run(provider.aclose())


def test_the_declared_secret_is_the_one_the_overlay_mounts_for_the_worker() -> None:
    import yaml

    provider = build_provider()
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

    provider = build_provider()
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


def test_build_provider_registers_the_strict_log_prefix() -> None:
    from vektra_shared.log_safety import is_strict_logger, strict_logger_prefixes

    provider = build_provider()
    asyncio.run(provider.aclose())
    assert vektra_lms_moodle.LOGGER_PREFIX in strict_logger_prefixes()
    assert is_strict_logger("vektra_lms_moodle.client")
    assert not is_strict_logger("vektra_lms_moodlex")


def test_the_provider_logs_are_redacted(canary_log_capture: Any) -> None:
    from vektra_lms_moodle import client

    build_provider()  # what the worker does (the conftest registered it as well)
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
    shared = {"VEKTRA_ENV", "VEKTRA_ASSESS_SECRETS_DIR"}
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


def test_the_default_call_deadline_fits_the_vertical_teacher_gate_budget() -> None:
    """The vertical budgets ``teacher_gate`` for a 30 s call deadline (it no longer reads this
    setting): the provider's default must not exceed it."""
    from vektra_assess.worker.policy import TEACHER_GATE_ASSUMED_CALL_DEADLINE_S

    default = MoodleSettings.model_fields["ws_total_timeout_s"].default
    assert default <= TEACHER_GATE_ASSUMED_CALL_DEADLINE_S


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
