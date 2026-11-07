"""VektraAssessSettings: dormant default, explicit aliases, no env file."""

from __future__ import annotations

from pathlib import Path

import pytest

from vektra_assess.config import VektraAssessSettings
from vektra_assess.errors import ERR_ASSESS_001, assess_unavailable


def test_defaults_are_dormant() -> None:
    s = VektraAssessSettings()
    assert s.enabled is False
    assert s.api_database_url is None and s.worker_database_url is None
    assert s.secrets_dir == Path("/run/secrets")
    assert (s.api_pool_size, s.worker_pool_size, s.heartbeat_max_age_s) == (15, 5, 60)


def test_reads_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_API_POOL_SIZE", "7")
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", "/tmp/x")
    s = VektraAssessSettings()
    assert (
        s.enabled is True and s.api_pool_size == 7 and s.secrets_dir == Path("/tmp/x")
    )


def test_field_names_are_not_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLED", "true")
    assert VektraAssessSettings().enabled is False


def test_ignores_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("VEKTRA_ASSESS_ENABLED=true\n")
    monkeypatch.chdir(tmp_path)
    assert VektraAssessSettings().enabled is False


def test_err_assess_001_is_503_and_names_the_item() -> None:
    from vektra_shared.errors import http_status_for

    err = assess_unavailable("worker_heartbeat")
    assert err.code == ERR_ASSESS_001 == "ERR-ASSESS-001"
    assert http_status_for(err) == 503
    assert "worker_heartbeat" in err.remediation


async def test_readiness_disabled_and_never_healthy_without_source() -> None:
    from vektra_assess.health import make_readiness_check

    off = await make_readiness_check(enabled=False)()
    assert off.status == "disabled"
    on = await make_readiness_check(enabled=True)()
    assert on.status == "degraded" and on.message == "heartbeat_source_missing"


def test_llm_defaults_and_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    s = VektraAssessSettings()
    assert s.llm_model is None and s.llm_temperature == 0.2
    assert s.outage_timeout_s == 45.0 and s.llm_price_per_1k_in is None
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "ollama/x")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_CONTEXT_WINDOW", "16384")
    monkeypatch.setenv("VEKTRA_ASSESS_OUTAGE_TIMEOUT_S", "30")
    s = VektraAssessSettings()
    assert (s.llm_model, s.llm_context_window, s.outage_timeout_s) == (
        "ollama/x",
        16384,
        30.0,
    )


@pytest.mark.parametrize(
    ("max_tokens", "window"), [("4096", "4096"), ("8192", "4096"), ("2048", "2048")]
)
def test_a_completion_reserve_that_fills_the_window_is_refused(
    monkeypatch: pytest.MonkeyPatch, max_tokens: str, window: str
) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MAX_TOKENS", max_tokens)
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_CONTEXT_WINDOW", window)
    with pytest.raises(ValidationError, match="LLM_MAX_TOKENS must be lower"):
        VektraAssessSettings()


def test_a_completion_reserve_below_the_window_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MAX_TOKENS", "2047")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_CONTEXT_WINDOW", "2048")
    s = VektraAssessSettings()
    assert s.llm_context_window - s.llm_max_tokens == 1


def test_an_unknown_site_timezone_blocks_only_an_enabled_assess() -> None:
    """CodeRabbit 4503025935: dormant assess never stops the API over its own settings."""
    import pytest
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings

    off = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_ENABLED": False, "VEKTRA_ASSESS_SITE_TIMEZONE": "Mars/Olympus"}
    )
    assert off.site_timezone == "Mars/Olympus"
    with pytest.raises(ValidationError, match="IANA"):
        VektraAssessSettings.model_validate(
            {
                "VEKTRA_ASSESS_ENABLED": True,
                "VEKTRA_ASSESS_SITE_TIMEZONE": "Mars/Olympus",
            }
        )
    ok = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_ENABLED": True, "VEKTRA_ASSESS_SITE_TIMEZONE": "Europe/Rome"}
    )
    assert ok.site_timezone == "Europe/Rome"
