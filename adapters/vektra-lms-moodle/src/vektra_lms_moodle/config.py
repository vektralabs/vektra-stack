"""The Moodle provider's settings (ADR-0033 D5, OD-4: ``VEKTRA_LMS_MOODLE_*``, no alias).

The provider reads its own environment, like the vertical reads ``VEKTRA_ASSESS_*``: the web-service
client's TLS, timeouts, byte caps and rate cap, the per-site breaker, the development-only plain-http
switch, the CIDRs an on-prem site may resolve into, and the ingestion account the setup check
refuses. Two names are shared with the platform and read, not owned: ``VEKTRA_ENV`` (the same
"development only when set explicitly" rule as the vertical) and ``VEKTRA_ASSESS_SECRETS_DIR``
(the directory where the worker's key matrix validated the provider's declared secrets, so the
provider reads its token file from the place that was checked).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MoodleSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True
    )

    env: str = Field(
        "development",
        alias="VEKTRA_ENV",
        description="Environment mode (shared with the platform). Plain http needs development, set explicitly.",
    )
    secrets_dir: Path = Field(
        Path("/run/secrets"),
        alias="VEKTRA_ASSESS_SECRETS_DIR",
        description="The worker's secrets directory, where the key matrix validated the token file.",
    )
    moodle_ca_file: Path | None = Field(
        None,
        alias="VEKTRA_LMS_MOODLE_CA_FILE",
        description="CA bundle used to verify the site's TLS certificate; the system store when unset.",
    )
    ws_timeout_connect_s: float = Field(
        3.0, gt=0, le=60, alias="VEKTRA_LMS_MOODLE_WS_TIMEOUT_CONNECT_S"
    )
    ws_timeout_read_s: float = Field(
        10.0, gt=0, le=120, alias="VEKTRA_LMS_MOODLE_WS_TIMEOUT_READ_S"
    )
    ws_total_timeout_s: float = Field(
        30.0,
        gt=0,
        le=600,
        alias="VEKTRA_LMS_MOODLE_WS_TOTAL_TIMEOUT_S",
        description="Overall deadline of one web-service call, body included: a server that drips bytes cannot hold it open.",
    )
    ws_max_response_bytes: int = Field(
        8 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
        alias="VEKTRA_LMS_MOODLE_WS_MAX_RESPONSE_BYTES",
        description="Byte cap of a JSON web-service response; the stream is aborted on overflow.",
    )
    ws_max_file_bytes: int = Field(
        10 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
        alias="VEKTRA_LMS_MOODLE_WS_MAX_FILE_BYTES",
        description="Byte cap of one downloaded submission file (REQ-101).",
    )
    ws_rps: float = Field(
        10.0,
        gt=0,
        le=1000,
        alias="VEKTRA_LMS_MOODLE_WS_RPS",
        description="Global cap on web-service calls per second, shared by every caller in the worker.",
    )
    breaker_threshold: int = Field(
        5, ge=1, le=100, alias="VEKTRA_LMS_MOODLE_BREAKER_THRESHOLD"
    )
    breaker_cooldown_s: float = Field(
        30.0, gt=0, le=3600, alias="VEKTRA_LMS_MOODLE_BREAKER_COOLDOWN_S"
    )
    moodle_allow_http: bool = Field(
        False,
        alias="VEKTRA_LMS_MOODLE_ALLOW_HTTP",
        description="Plain http to the site. Honoured only with VEKTRA_ENV=development; the worker refuses to start otherwise.",
    )
    moodle_allowed_cidrs: str = Field(
        "",
        alias="VEKTRA_LMS_MOODLE_ALLOWED_CIDRS",
        description="Comma-separated CIDRs a site host may resolve into (an on-prem site on a private network). Empty: private addresses are refused.",
    )
    ingest_account_username: str | None = Field(
        None,
        alias="VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME",
        description="Username of the ingestion web-service account; the setup check refuses the assess token if it belongs to it. Unset: that item is UNVERIFIABLE.",
    )

    @property
    def is_development(self) -> bool:
        """True only when ``VEKTRA_ENV=development`` was SET, never when it merely defaulted
        (the vertical's rule, ``VektraAssessSettings.is_development``)."""
        return self.env == "development" and "env" in self.model_fields_set

    @property
    def env_described(self) -> str:
        """The environment as a refusal message states it."""
        if "env" not in self.model_fields_set or not self.env.strip():
            return (
                "VEKTRA_ENV unset; the default 'development' does not count: "
                "set VEKTRA_ENV=development explicitly"
            )
        return f"VEKTRA_ENV={self.env}"


__all__ = ["MoodleSettings"]
