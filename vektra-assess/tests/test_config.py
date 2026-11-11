"""VektraAssessSettings: dormant default, explicit aliases, no env file."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize(
    "bad",
    [
        "Vektra <vektra@uni.example>",
        "<vektra@uni.example>",
        "vektra @uni.example",
        "vektra@uni.example\r\nBcc: x@evil.example",
        "vektra@uni.example, other@uni.example",
        "vektra",
    ],
)
def test_the_smtp_from_is_a_bare_address(bad: str) -> None:
    """Plan 10b review #10: the From is an addr-spec, never a display name or a header."""
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings

    with pytest.raises(ValidationError, match="VEKTRA_ASSESS_SMTP_FROM"):
        VektraAssessSettings.model_validate({"VEKTRA_ASSESS_SMTP_FROM": bad})
    ok = VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_SMTP_FROM": " Vektra@Uni.EXAMPLE "}
    )
    assert ok.smtp_from == "Vektra@uni.example"


def test_a_malformed_operator_email_blocks_only_an_enabled_assess() -> None:
    """CodeRabbit 4513956271: a dormant feature's setting never stops the API from starting.
    Enabled: a malformed address is refused at startup and a good one is normalised."""
    from pydantic import ValidationError

    from vektra_assess.config import VektraAssessSettings

    bad = "Ops <ops@uni.example>"
    with pytest.warns(UserWarning, match="VEKTRA_ASSESS_OPERATOR_EMAIL"):
        off = VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_ENABLED": False, "VEKTRA_ASSESS_OPERATOR_EMAIL": bad}
        )
    assert off.operator_email == bad  # kept as given, never used while dormant
    with pytest.raises(ValidationError, match="VEKTRA_ASSESS_OPERATOR_EMAIL"):
        VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_ENABLED": True, "VEKTRA_ASSESS_OPERATOR_EMAIL": bad}
        )
    ok = VektraAssessSettings.model_validate(
        {
            "VEKTRA_ASSESS_ENABLED": True,
            "VEKTRA_ASSESS_OPERATOR_EMAIL": " Ops@Uni.EXAMPLE ",
        }
    )
    assert ok.operator_email == "Ops@uni.example"


# --- plan assess-teacher-ops: startup refusals ------------------------------------------------


def test_rag_flag_on_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCH-072: no dormant RAG path; the flag exists only to be refused."""
    assert VektraAssessSettings().rag_enabled is False
    monkeypatch.setenv("VEKTRA_ASSESS_RAG_ENABLED", "true")
    with pytest.raises(ValidationError, match="not available in this release"):
        VektraAssessSettings()


def test_reset_notice_flag_on_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-092 is out of v1 (product owner 2026-11-08): no dormant notice path; the flag
    exists only to be refused, whatever text version is named."""
    assert VektraAssessSettings().reset_notice_enabled is False
    monkeypatch.setenv("VEKTRA_ASSESS_RESET_NOTICE_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_RESET_NOTICE_TEXT_VERSION", "dpo-2026-12-01")
    with pytest.raises(ValidationError, match="not available in this release"):
        VektraAssessSettings()


def test_every_numeric_operator_setting_has_both_bounds() -> None:
    """REQ-080 / plan 12b: no operator-facing number is unbounded on either side."""
    import typing

    import annotated_types as at

    loose = []
    for name, f in VektraAssessSettings.model_fields.items():
        args = typing.get_args(f.annotation) or (f.annotation,)
        if not any(a in (int, float) for a in args):
            continue
        lo = any(isinstance(m, (at.Ge, at.Gt)) for m in f.metadata)
        hi = any(isinstance(m, (at.Le, at.Lt)) for m in f.metadata)
        if not (lo and hi):
            loose.append(f.alias or name)
    assert loose == []


def test_a_value_out_of_bounds_names_the_variable_in_the_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess.config import explain_settings_error

    monkeypatch.setenv("VEKTRA_ASSESS_BREAKER_THRESHOLD", "100000")
    monkeypatch.setenv("VEKTRA_ASSESS_API_POOL_SIZE", "0")
    with pytest.raises(ValidationError) as exc:
        VektraAssessSettings()
    detail, remediation = explain_settings_error(exc.value)
    assert "VEKTRA_ASSESS_BREAKER_THRESHOLD" in detail
    assert "VEKTRA_ASSESS_API_POOL_SIZE" in detail
    assert "100000" not in detail  # the bound is named, the value is not echoed
    assert "VEKTRA_ASSESS_BREAKER_THRESHOLD" in remediation
    assert "docs/reference/configuration.md" in remediation


def test_a_field_validator_message_is_never_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeRabbit 4524508948: a field validator's message can quote the rejected text (the
    retry schedule does); only bound and type messages, which never carry the input, are kept."""
    from vektra_assess.config import explain_settings_error

    secret = "hunter2-very-secret"
    monkeypatch.setenv("VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE", secret)
    monkeypatch.setenv("VEKTRA_ASSESS_API_POOL_SIZE", "0")
    with pytest.raises(ValidationError) as exc:
        VektraAssessSettings()
    detail, _ = explain_settings_error(exc.value)
    assert secret not in detail
    assert "VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE: invalid value" in detail
    assert (
        "VEKTRA_ASSESS_API_POOL_SIZE: Input should be greater than or equal to 1"
        in detail
    )


# --- development is explicit (assess only; the platform default stays "development") ---------


def test_development_must_be_set_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEKTRA_ENV", raising=False)
    defaulted = VektraAssessSettings()
    assert defaulted.env == "development"  # the shared default, unchanged
    assert defaulted.is_development is False
    assert "VEKTRA_ENV unset" in defaulted.env_described
    monkeypatch.setenv("VEKTRA_ENV", "development")
    assert VektraAssessSettings().is_development is True
    monkeypatch.setenv("VEKTRA_ENV", "production")
    prod = VektraAssessSettings()
    assert (
        prod.is_development is False and prod.env_described == "VEKTRA_ENV=production"
    )
    explicit = VektraAssessSettings.model_validate({"VEKTRA_ENV": "development"})
    assert explicit.is_development is True


def test_the_erasure_default_needs_an_explicit_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_assess.config import erasure_mode_problem

    monkeypatch.delenv("VEKTRA_ENV", raising=False)
    on = {"VEKTRA_ASSESS_ENABLED": "true"}
    assert erasure_mode_problem(VektraAssessSettings.model_validate(on)) is not None
    dev = VektraAssessSettings.model_validate({**on, "VEKTRA_ENV": "development"})
    assert erasure_mode_problem(dev) is None


# --- LMS provider name (ADR-0033, plan 2a-1.6) ------------------------------------------------


def test_the_lms_provider_is_unset_by_default_and_read_by_its_alias() -> None:
    assert VektraAssessSettings().lms_provider is None
    s = VektraAssessSettings.model_validate({"VEKTRA_ASSESS_LMS_PROVIDER": "example"})
    assert s.lms_provider == "example"
    blank = VektraAssessSettings.model_validate({"VEKTRA_ASSESS_LMS_PROVIDER": " "})
    assert blank.lms_provider is None  # an empty compose value means unset


@pytest.mark.parametrize("bad", ["Example", "a b", "x" * 65, "../evil", "1abc"])
def test_a_malformed_lms_provider_name_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        VektraAssessSettings.model_validate({"VEKTRA_ASSESS_LMS_PROVIDER": bad})
