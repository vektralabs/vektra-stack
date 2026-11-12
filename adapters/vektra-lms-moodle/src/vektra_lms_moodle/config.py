"""The Moodle provider's settings (ADR-0033 D5, OD-4: ``VEKTRA_LMS_MOODLE_*``, no alias).

The provider reads its own environment, like the vertical reads ``VEKTRA_ASSESS_*``: the web-service
client's TLS, timeouts, byte caps and rate cap, the per-site breaker, the development-only plain-http
switch, the CIDRs an on-prem site may resolve into, and the ingestion account the setup check
refuses. One name is shared with the platform and read, not owned: ``VEKTRA_ASSESS_SECRETS_DIR``
(the directory where the worker's key matrix validated the provider's declared secrets, so the
provider reads its token file from the place that was checked). Development mode is NOT read
here: the composition root passes its resolved flag to the entry point (``development``).

Only the aliases are read (``populate_by_name=False``, as ``VektraAssessSettings``): a bare
``MOODLE_ALLOW_HTTP`` or ``WS_RPS`` in a container's environment configures nothing. The names
the vertical used before the provider left it are refused, not ignored (``legacy_names``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MoodleSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=False
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


#: the names these settings had in the vertical before 2a-4 moved them here (OD-4: no alias). A
#: value under one of them would be silently ignored, so the provider refuses to start instead
#: (review of #207, finding 4). Exact names for the ``VEKTRA_ASSESS_MOODLE_*`` family: the live
#: contract suites' own variables (``VEKTRA_ASSESS_MOODLE43_URL``, ``..._MANIFEST_DIR``, ...) share
#: that prefix and are test inputs, not settings.
_LEGACY_EXACT = {
    "VEKTRA_ASSESS_MOODLE_CA_FILE": "VEKTRA_LMS_MOODLE_CA_FILE",
    "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP": "VEKTRA_LMS_MOODLE_ALLOW_HTTP",
    "VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS": "VEKTRA_LMS_MOODLE_ALLOWED_CIDRS",
    "VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME": "VEKTRA_LMS_MOODLE_INGEST_ACCOUNT_USERNAME",
}
#: whole families: every ``VEKTRA_ASSESS_WS_*`` and ``VEKTRA_ASSESS_BREAKER_*`` was the client's
#: (the vertical's own per-site Start breaker is ``VEKTRA_ASSESS_START_BREAKER_*``)
_LEGACY_PREFIXES = {
    "VEKTRA_ASSESS_WS_": "VEKTRA_LMS_MOODLE_WS_",
    "VEKTRA_ASSESS_BREAKER_": "VEKTRA_LMS_MOODLE_BREAKER_",
}


def legacy_names(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(old, new)`` for every pre-2a-4 setting name set in ``environ``, sorted; names only."""
    found: dict[str, str] = {}
    for key in environ:
        if key in _LEGACY_EXACT:
            found[key] = _LEGACY_EXACT[key]
            continue
        for old, new in _LEGACY_PREFIXES.items():
            if key.startswith(old):
                found[key] = new + key[len(old) :]
    return sorted(found.items())


def load_settings() -> MoodleSettings:
    """The settings from the environment, or a ``ProviderConfigurationError`` that names each
    rejected variable and its rule, never its value (``vektra_shared.settings_errors``, the
    vertical's own explanation)."""
    from pydantic import ValidationError

    from vektra_shared.lms import ProviderConfigurationError
    from vektra_shared.settings_errors import explain_settings_error

    try:
        return MoodleSettings()
    except ValidationError as exc:
        detail, remediation = explain_settings_error(
            exc,
            MoodleSettings,
            fallback="the VEKTRA_LMS_MOODLE_* variables",
            docs="docs/reference/configuration.md (Moodle LMS provider)",
        )
        raise ProviderConfigurationError(detail, remediation) from None


__all__ = ["MoodleSettings", "legacy_names", "load_settings"]
