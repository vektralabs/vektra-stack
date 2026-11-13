"""Assess settings (VektraAssessSettings).

Every assess setting is read here, with an explicit ``VEKTRA_ASSESS_*`` alias and no
env_file: the assess processes read their environment and their secret files, nothing
else. Later plans add their fields to this class only. Secrets never live here: they are
keyring files under ``SECRETS_DIR`` (``vektra_assess.keys``).

Owning plan: .s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

import json
import re
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from vektra_assess.privacy_text import notice_version_error

# --- pure rules shared with vektra_assess.delivery (plan transcript-delivery 10b) -------------
# They live HERE, not in vektra_assess.delivery: validating the settings must not import a
# module outside the parser's import ceiling (the parser sidecar loads this class too).

MAX_ADDRESS_BYTES = 254

_LOCAL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


def normalize_address(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate: not text an SMTP server can carry
        return None
    if not value or size > MAX_ADDRESS_BYTES:
        return None
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    domain = domain.lower()
    # fullmatch: ``$`` alone also matches before a trailing newline
    if len(local) > 64 or not _LOCAL.fullmatch(local) or not _DOMAIN.fullmatch(domain):
        return None
    return f"{local}@{domain}"


_STEP = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_schedule(text: str) -> tuple[timedelta, ...]:
    steps: list[timedelta] = []
    for part in str(text).split(","):
        match = _STEP.match(part)
        if match is None:
            raise ValueError(f"not a duration: {part.strip()!r} (use e.g. 30s, 5m, 1h)")
        seconds = int(match.group(1)) * _UNIT[match.group(2)]
        if seconds <= 0:
            raise ValueError("every step of the retry schedule must be positive")
        steps.append(timedelta(seconds=seconds))
    if not steps:
        raise ValueError("the retry schedule is empty")
    return tuple(steps)


class VektraAssessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=False, frozen=True
    )

    enabled: bool = Field(
        False,
        alias="VEKTRA_ASSESS_ENABLED",
        description=(
            "Master switch. False (default): no /api/v1/assess route is mounted and the "
            "assess-worker / assess-parser entrypoints refuse to run."
        ),
    )
    api_database_url: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_API_DATABASE_URL",
        description="DSN of the vektra_assess_api role (never the superuser DSN).",
    )
    worker_database_url: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_WORKER_DATABASE_URL",
        description="DSN of the vektra_assess_worker role (worker process only).",
    )
    secrets_dir: Path = Field(
        Path("/run/secrets"),
        alias="VEKTRA_ASSESS_SECRETS_DIR",
        description="Directory holding the assess keyring files (Docker secrets).",
    )
    api_pool_size: int = Field(15, ge=1, le=100, alias="VEKTRA_ASSESS_API_POOL_SIZE")
    worker_pool_size: int = Field(
        5, ge=1, le=50, alias="VEKTRA_ASSESS_WORKER_POOL_SIZE"
    )
    heartbeat_max_age_s: int = Field(
        60,
        ge=30,  # at least three beat intervals (10 s), or /health/assess would flap
        le=600,
        alias="VEKTRA_ASSESS_HEARTBEAT_MAX_AGE_S",
        description="A worker heartbeat older than this makes /health/assess non-healthy.",
    )
    worker_heartbeat_file: Path = Field(
        Path("/tmp/vektra-assess-worker.heartbeat"),
        alias="VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE",
        description="File whose mtime the worker refreshes every VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S (container healthcheck).",
    )
    worker_heartbeat_interval_s: float = Field(
        5.0,
        gt=0,
        le=10,  # heartbeat_max_age_s (>= 30) must stay at least three beats
        alias="VEKTRA_ASSESS_WORKER_HEARTBEAT_INTERVAL_S",
        description="Seconds between worker heartbeats (row upsert plus the tmpfs file touch).",
    )
    worker_allow_unbound: bool = Field(
        False,
        alias="VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND",
        description=(
            "Development and CI only: let the worker start while some job kinds or ticks have no "
            "handler yet (they stay unbound and a job of that kind fails with handler_not_bound, "
            "never completes silently). Honoured only with VEKTRA_ENV=development; the worker "
            "refuses to start with it set in any other environment."
        ),
    )
    worker_db_command_timeout_s: float = Field(
        30.0,
        gt=0,
        le=300,
        alias="VEKTRA_ASSESS_WORKER_DB_COMMAND_TIMEOUT_S",
        description=(
            "Upper bound on any single worker database statement. A database that restarts or a "
            "network that drops can leave a connection half-open (no FIN, no error, just silence); "
            "without a bound the worker would wait on it forever. After it the connection is "
            "discarded and replaced."
        ),
    )
    worker_wedge_exit_s: float = Field(
        60.0,
        ge=10,
        le=600,
        alias="VEKTRA_ASSESS_WORKER_WEDGE_EXIT_S",
        description=(
            "Liveness watchdog: if the worker has not managed to write its heartbeat row for this "
            "long while it should be ready, it exits with code 70 and the supervisor "
            "(restart: unless-stopped) starts a fresh process, which reconciles on startup. A "
            "database connection that went half-open (restart, dropped network) can leave a "
            "process alive but doing nothing; this is the backstop for any such wedge."
        ),
    )
    job_poll_interval_s: float = Field(
        1.0,
        gt=0,
        le=10,
        alias="VEKTRA_ASSESS_JOB_POLL_INTERVAL_S",
        description="Claim-loop poll when no NOTIFY arrives; the correctness backstop, NOTIFY is only a latency hint.",
    )
    job_backoff_base_s: float = Field(
        5.0, gt=0, le=3600, alias="VEKTRA_ASSESS_JOB_BACKOFF_BASE_S"
    )
    job_backoff_cap_s: float = Field(
        300.0, gt=0, le=86_400, alias="VEKTRA_ASSESS_JOB_BACKOFF_CAP_S"
    )
    job_done_retention_h: int = Field(
        24,
        ge=1,
        le=8760,
        alias="VEKTRA_ASSESS_JOB_DONE_RETENTION_H",
        description="Hours a done job row is kept before the hourly cleanup tick deletes it; dead rows are kept for the purge plan.",
    )
    worker_drain_s: float = Field(
        20.0,
        gt=0,
        le=25,  # below the Compose stop_grace_period of 30 s
        alias="VEKTRA_ASSESS_WORKER_DRAIN_S",
        description="On SIGTERM, seconds to wait for in-flight handlers before cancelling them and releasing their leases.",
    )
    parser_socket: Path = Field(
        Path("/run/vektra-assess-parser/parser.sock"),
        alias="VEKTRA_ASSESS_PARSER_SOCKET",
        description="Unix socket of the parser sidecar (worker and parser only).",
    )
    parser_pool: int = Field(
        3,
        ge=1,
        le=16,
        alias="VEKTRA_ASSESS_PARSER_POOL",
        description="Concurrent child processes of the parser sidecar (memory budget: 768M container).",
    )
    parser_timeout_s: float = Field(
        10.0,
        ge=0.5,
        le=120,
        alias="VEKTRA_ASSESS_PARSER_TIMEOUT_S",
        description="Wall-clock budget per file in the sidecar; the child is SIGKILLed after it. The worker client allows this plus a queue allowance.",
    )
    parser_max_chars: int = Field(
        200_000,
        ge=1000,
        le=1_000_000,
        alias="VEKTRA_ASSESS_PARSER_MAX_CHARS",
        description="Character cap of the text returned per file (READ_PARTIAL beyond it).",
    )
    parser_max_file_bytes: int = Field(
        10 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
        alias="VEKTRA_ASSESS_PARSER_MAX_FILE_BYTES",
        description="Largest file the sidecar accepts (REQ-101: 10 MB); bigger is TOO_LARGE, no child forked. The sidecar also holds at most four such payloads in memory at once.",
    )
    parser_max_pages: int = Field(
        50,
        ge=1,
        le=500,
        alias="VEKTRA_ASSESS_PARSER_MAX_PAGES",
        description="PDF page cap (REQ-101: 50); a client cannot ask for more.",
    )
    parser_rlimit_as_mb: int = Field(
        256,
        ge=64,
        le=2048,
        alias="VEKTRA_ASSESS_PARSER_RLIMIT_AS_MB",
        description="RLIMIT_AS of each parsing child, in MiB.",
    )

    # --- environment mode -------------------------------------------------------------------
    # The LMS client's own settings (TLS, timeouts, caps, breaker, plain-http switch) belong to
    # the LMS provider, which reads them itself (ADR-0033 D5, OD-4).
    env: str = Field(
        "development",
        alias="VEKTRA_ENV",
        description="Environment mode (shared with the main settings). Development-only switches need it set explicitly.",
    )

    @property
    def is_development(self) -> bool:
        """True only when ``VEKTRA_ENV=development`` was SET, never when it merely defaulted.

        The platform default is ``development`` (``vektra_shared.config``, shared with every
        component and left as is), so a production ``.env`` that forgets ``VEKTRA_ENV`` would
        otherwise honour every assess development-only switch. Every such switch asks this.
        """
        return self.env == "development" and "env" in self.model_fields_set

    @property
    def env_described(self) -> str:
        """The environment as a refusal message states it."""
        # empty is unset too: compose passes ${VEKTRA_ENV:-} to the processes without env_file
        if "env" not in self.model_fields_set or not self.env.strip():
            return (
                "VEKTRA_ENV unset; the default 'development' does not count for assess: "
                "set VEKTRA_ENV=development explicitly"
            )
        return f"VEKTRA_ENV={self.env}"

    ui_origin: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_UI_ORIGIN",
        description="Origin the chat UI is served from; the setup check refuses it if it is the same site as the LMS base_url or the API public origin (CONF-004). Unset: UNVERIFIABLE.",
    )
    api_public_origin: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_API_PUBLIC_ORIGIN",
        description="Public origin of the assess API, compared with the UI origin by the setup check.",
    )
    # --- discussion runner (plan assess-llm-runner) -----------------------------------------
    llm_model: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_LLM_MODEL",
        description="Dedicated model of the discussion runner (litellm id). No fallback list: a fallback model would bypass the NFR-023 activation gate.",
    )
    llm_temperature: float = Field(
        0.2, ge=0, le=2, alias="VEKTRA_ASSESS_LLM_TEMPERATURE"
    )
    llm_max_tokens: int = Field(
        600,
        ge=64,
        le=8192,
        alias="VEKTRA_ASSESS_LLM_MAX_TOKENS",
        description="Completion tokens per reply; reserved out of the context window before history is kept.",
    )
    llm_context_window: int = Field(
        8192,
        ge=2048,
        le=1_048_576,
        alias="VEKTRA_ASSESS_LLM_CONTEXT_WINDOW",
        description="Context window of the model in tokens. History is trimmed oldest first to fit; the context bundle is never trimmed (a bundle that does not fit is an error).",
    )

    @model_validator(mode="after")
    def _completion_fits_the_window(self) -> Self:
        """The completion reserve must leave a positive prompt budget in the context window."""
        if self.llm_max_tokens >= self.llm_context_window:
            raise ValueError(
                "VEKTRA_ASSESS_LLM_MAX_TOKENS must be lower than VEKTRA_ASSESS_LLM_CONTEXT_WINDOW: "
                "the completion is reserved out of the window and the prompt needs the rest"
            )
        return self

    llm_seed: int | None = Field(
        None,
        ge=0,
        le=2**31 - 1,
        alias="VEKTRA_ASSESS_LLM_SEED",
        description="Passed to the provider where it supports a seed (REQ-100 determinism).",
    )
    outage_timeout_s: float = Field(
        45.0,
        gt=0,
        le=300,
        alias="VEKTRA_ASSESS_OUTAGE_TIMEOUT_S",
        description="No first token within this many seconds is a provider outage (REQ-086); a slow stream that keeps producing is not.",
    )
    llm_price_per_1k_in: float | None = Field(
        None, ge=0, le=1000, alias="VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN"
    )
    llm_price_per_1k_out: float | None = Field(
        None, ge=0, le=1000, alias="VEKTRA_ASSESS_LLM_PRICE_PER_1K_OUT"
    )
    store_traces: bool = Field(
        True,
        alias="VEKTRA_ASSESS_STORE_TRACES",
        description="Store the runner's content-free QueryTrace per turn and per Part B (pipeline 'assess-discussion'), independent of VEKTRA_ANALYTICS store settings.",
    )
    llm_api_base: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_LLM_API_BASE",
        description="API base of the discussion model (OpenAI-compatible, vLLM or Ollama endpoint). Its key is VEKTRA_LLM_API_KEY (shared with the RAG); a cloud key may also come from litellm's own provider variable.",
    )
    llm_api_key: SecretStr | None = Field(
        None,
        alias="VEKTRA_LLM_API_KEY",
        description=(
            "API key of the discussion model's endpoint. Deliberately the SAME variable the RAG "
            "pipeline uses (decision of 2026-11-06, OQ-026: one key for the on-prem vLLM, no new "
            "secret). Read by the API and by the worker (Part B); an empty value means no key."
        ),
    )
    llm_extra_body: Annotated[dict[str, Any] | None, NoDecode] = Field(
        None,
        alias="VEKTRA_ASSESS_LLM_EXTRA_BODY",
        description=(
            "JSON object passed as extra_body on every discussion-model call, e.g. "
            '{"chat_template_kwargs": {"enable_thinking": false}} for Qwen3 on vLLM. Empty or '
            "unset: none. Never the RAG's VEKTRA_LLM_EXTRA_BODY."
        ),
    )

    @model_validator(mode="after")
    def _known_zone_when_enabled(self) -> VektraAssessSettings:
        """An unknown zone is refused only while assess is ENABLED: a dormant feature must not
        stop the API from starting."""
        if not self.enabled:
            return self
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.site_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "VEKTRA_ASSESS_SITE_TIMEZONE is not a known IANA zone"
            ) from None
        return self

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def _parse_extra_body(cls, value: Any) -> Any:
        """Compose passes ``${VAR:-}`` as an empty string: that is "none", not a JSON error."""
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(
                    "VEKTRA_ASSESS_LLM_EXTRA_BODY is not valid JSON"
                ) from None
        if value is not None and not isinstance(value, dict):
            raise ValueError("VEKTRA_ASSESS_LLM_EXTRA_BODY must be a JSON object")
        return value

    @field_validator("llm_api_base", mode="before")
    @classmethod
    def _empty_base_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    safeguard_mode: Literal["presidio", "none"] = Field(
        "presidio",
        alias="VEKTRA_ASSESS_SAFEGUARD_MODE",
        description="Safeguard of the discussion runner in the API. 'presidio' fails startup if Presidio cannot load (no silent passthrough); 'none' must be set explicitly.",
    )
    provider_locality: Literal["onprem", "cloud"] = Field(
        "cloud",
        alias="VEKTRA_ASSESS_PROVIDER_LOCALITY",
        description="Where the discussion model runs. 'cloud' (the safe default) makes the eval adapter refuse any dataset whose data_basis is not 'synthetic' before a single request (NFR-022 data basis), and makes the API refuse VEKTRA_ASSESS_SAFEGUARD_MODE=none outside development; set 'onprem' only for a model on the institution's own hardware.",
    )
    # --- LMS provider (ADR-0033, plan 2a-1.6) -------------------------------------------------
    lms_provider: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_LMS_PROVIDER",
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
        description="Name of the LMS provider the worker loads from the entry-point group vektra.lms_providers (ADR-0033 D2), the name an installed provider distribution declares. Required with VEKTRA_ASSESS_ENABLED=true: unset, the worker refuses to start and lists the installed providers.",
    )

    @field_validator("lms_provider", mode="before")
    @classmethod
    def _empty_provider_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # --- student session lifecycle (plan assess-session-lifecycle) ---------------------------
    token_issuer: str = Field(
        "vektra-assess",
        min_length=1,
        alias="VEKTRA_ASSESS_TOKEN_ISSUER",
        description="iss claim of every discussion token (aud is always 'discussion', ARCH-067).",
    )
    presession_ttl_s: int = Field(
        120,
        ge=10,
        le=120,  # ARCH-067: a pre-session token lives at most 120 s; a larger value refuses startup
        alias="VEKTRA_ASSESS_PRESESSION_TTL_S",
        description="Lifetime of a pre-session (mint) token, at most 120 s.",
    )
    session_token_ttl_s: int = Field(
        900,
        ge=60,
        le=86_400,
        alias="VEKTRA_ASSESS_SESSION_TOKEN_TTL_S",
        description="Upper bound of a session-bound token's lifetime; it also never outlives deadline + reply grace.",
    )
    reply_grace_s: int = Field(
        60,
        ge=0,
        le=600,
        alias="VEKTRA_ASSESS_REPLY_GRACE_S",
        description="Seconds after the deadline an in-flight reply may still finish (REQ-070).",
    )
    min_plugin_version: str = Field(
        "0.1.0",
        min_length=1,
        alias="VEKTRA_ASSESS_MIN_PLUGIN_VERSION",
        description="Lowest local_vektradiscuss release accepted at mint (ARCH-067); a site's own minimum applies when higher.",
    )
    start_t_s: int = Field(
        60,
        ge=20,
        le=300,
        alias="VEKTRA_ASSESS_START_T_S",
        description="REQ-071 commit window T: a Start not committed within T of the request is rolled back.",
    )
    admission_k: int = Field(
        10,
        ge=1,
        le=1000,
        alias="VEKTRA_ASSESS_ADMISSION_K",
        description="NFR-020 admission limit: live sessions (ACTIVE, OUTAGE, STARTING in pre-flight) admitted at once.",
    )
    privacy_notice_version: str = Field(
        "1",
        min_length=1,
        max_length=40,
        alias="VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION",
        description="Version of the privacy notice shown before Start; recorded on the session and the start audit event.",
    )

    @field_validator("privacy_notice_version")
    @classmethod
    def _notice_version_rule(cls, value: str) -> str:
        """The stand-in ``GET /state`` shows for a site with no settings meets the rule the
        plugin's echo is checked with (plugin request P3): never a value the mint would refuse."""
        code = notice_version_error(value)
        if code is not None:
            raise ValueError(
                f"VEKTRA_ASSESS_PRIVACY_NOTICE_VERSION is invalid ({code}): 1 to 40 "
                "characters, not blank, no control character other than tab and newline"
            )
        return value

    privacy_notice_url: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_PRIVACY_NOTICE_URL",
        description="URL of the privacy notice the plugin links on the pre-session screen (GET /state pre_session.privacy).",
    )
    render_fresh_s: int = Field(
        300,
        ge=10,
        le=86_400,
        alias="VEKTRA_ASSESS_RENDER_FRESH_S",
        description=(
            "Age under which a worker-computed button state (GET /state) is fresh. Older or "
            "missing: /state answers 'unavailable' / 'checking' and queues a render check; a Start "
            "is refused only on a FRESH negative state (the pre-flight is authoritative)."
        ),
    )
    state_rpm: int = Field(
        30,
        ge=1,
        le=6000,
        alias="VEKTRA_ASSESS_STATE_RPM",
        description="GET /state calls per minute per (site, student, activity).",
    )
    enrolment_freshness_s: int = Field(
        150,
        ge=60,
        le=3600,
        alias="VEKTRA_ASSESS_ENROLMENT_FRESHNESS_S",
        description=(
            "A turn needs an 'enrolled' re-check no older than this (gate step 6). The worker "
            "re-checks every 60 s; a worker stopped longer than this rejects turns (fail closed)."
        ),
    )
    start_rate_per_hour: int = Field(
        5,
        ge=1,
        le=100,
        alias="VEKTRA_ASSESS_START_RATE_PER_HOUR",
        description="REQ-071: Start attempts per (student, activity) per hour; beyond it 429 'Riprova tra X minuti'.",
    )
    rollback_notify_after: int = Field(
        3,
        ge=1,
        le=20,
        alias="VEKTRA_ASSESS_ROLLBACK_NOTIFY_AFTER",
        description="REQ-071: consecutive system rollbacks of one (student, activity) that notify teacher and operator ('Problema tecnico persistente').",
    )
    start_breaker_rollbacks: int = Field(
        10,
        ge=2,
        le=1000,
        alias="VEKTRA_ASSESS_START_BREAKER_ROLLBACKS",
        description="Per-site Start breaker: system rollbacks within the window that open it.",
    )
    start_breaker_window_s: int = Field(
        300,
        ge=30,
        le=86_400,
        alias="VEKTRA_ASSESS_START_BREAKER_WINDOW_S",
        description="Per-site Start breaker: the window the rollbacks are counted in.",
    )
    start_breaker_cooldown_s: int = Field(
        120,
        ge=10,
        le=86_400,
        alias="VEKTRA_ASSESS_START_BREAKER_COOLDOWN_S",
        description="Per-site Start breaker: seconds fully open before one Start at a time is let through (half-open).",
    )
    ui_page_rpm: int = Field(
        60,
        ge=1,
        le=6000,
        alias="VEKTRA_ASSESS_UI_PAGE_RPM",
        description="GET /api/v1/assess/ui (the public discussion page) per minute per client address (plan 9). Over it: 429 before any database read.",
    )
    flag_rpm: int = Field(
        10,
        ge=1,
        le=600,
        alias="VEKTRA_ASSESS_FLAG_RPM",
        description="POST /sessions/{sid}/turns/{turn_no}/flag calls per minute per session (REQ-103).",
    )

    @field_validator("ui_origin", mode="before")
    @classmethod
    def _ui_origin_blank_is_unset(cls, value: Any) -> Any:
        return None if value in (None, "") else str(value).strip()

    @model_validator(mode="after")
    def _ui_origin_is_bare(self) -> Self:
        """The UI origin, normalised (``ui.origins.bare_origin``). Malformed: refused while
        assess is enabled; while it is disabled only a warning, the value kept as given and
        never used (a dormant feature's setting never stops the API from starting)."""
        if not self.ui_origin:
            return self
        from vektra_assess.ui.origins import bare_origin

        try:
            # the settings are frozen: the normalised value replaces the raw one at load only
            object.__setattr__(self, "ui_origin", bare_origin(self.ui_origin))
        except ValueError as exc:
            if self.enabled:
                raise ValueError(
                    f"VEKTRA_ASSESS_UI_ORIGIN is not a bare http(s) origin: {exc}"
                ) from exc
            warnings.warn(
                "VEKTRA_ASSESS_UI_ORIGIN is not a bare http(s) origin; ignored while "
                "VEKTRA_ASSESS_ENABLED=false, refused once it is enabled",
                UserWarning,
                stacklevel=2,
            )
        return self

    retry_rpm: int = Field(
        6,
        ge=1,
        le=600,
        alias="VEKTRA_ASSESS_RETRY_RPM",
        description="POST /sessions/{sid}/retry calls per minute per session (REQ-086).",
    )
    outage_g_s: int = Field(
        20,
        ge=0,
        le=300,
        alias="VEKTRA_ASSESS_OUTAGE_G_S",
        description=(
            "REQ-086 G: an outage shorter than this earns no time back; a longer one is credited "
            "in full, the cumulative credit capped at C = min(15 min, half the duration)."
        ),
    )
    messages_per_min: int = Field(
        4,
        ge=1,
        le=60,
        alias="VEKTRA_ASSESS_MESSAGES_PER_MIN",
        description=(
            "Student messages per minute per session (REQ-083, M5.1); above it 429 'Attendi "
            "qualche secondo', before any model call. One reply at a time already holds a real "
            "student well under it."
        ),
    )
    session_token_budget: int = Field(
        500_000,
        ge=10_000,
        le=10_000_000,
        alias="VEKTRA_ASSESS_SESSION_TOKEN_BUDGET",
        description=(
            "Estimated tokens (prompt + completion, TurnUsage) one session may use (REQ-083, "
            "M5.1): budget_warning at 80%, the session closes budget_exhausted at 100%. Outage "
            "retries and earlier sessions do not count."
        ),
    )
    max_message_chars: int = Field(
        2000,
        ge=1,
        le=20_000,
        alias="VEKTRA_ASSESS_MAX_MESSAGE_CHARS",
        description=(
            "Longest student message accepted (REQ-083); longer or blank is a 400 before any model "
            "call. Sized in M5.1: about 25x the longest eval-set question (76 chars), room for a "
            "multi-paragraph argument, about 570 tokens (well inside the prompt budget)."
        ),
    )
    privacy_allow_unset: bool = Field(
        False,
        alias="VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET",
        description=(
            "Development and CI only: let the Start gate BR-006 (h) pass while the REQ-089 privacy "
            "configuration does not exist yet (its store arrives with plan assess-privacy-retention; "
            "until then the gate fails closed and every Start is rolled back with "
            "privacy_not_configured). Honoured only with VEKTRA_ENV=development."
        ),
    )
    # --- retention, erasure, export (plan assess-privacy-retention) -------------------------
    erasure_mode: Literal["erase", "retain_exam_evidence"] | None = Field(
        None,
        alias="VEKTRA_ASSESS_ERASURE_MODE",
        description=(
            "OQ-033 / OQ-020 (the DPO's decision): 'erase' deletes a subject's discussions on an "
            "erasure request; 'retain_exam_evidence' keeps them under the configured legal basis "
            "and answers outcome=retained (audited, nothing deleted). Mandatory while assess is "
            "enabled outside development (the API refuses to start without it): no implicit "
            "choice of the DPO's decision."
        ),
    )
    privacy_rpm: int = Field(
        30,
        ge=1,
        le=600,
        alias="VEKTRA_ASSESS_PRIVACY_RPM",
        description=(
            "Privacy export, listing and erasure requests per minute per key (the plugin's "
            "privacy provider); 429 beyond it."
        ),
    )
    erasure_wait_s: float = Field(
        4.0,
        ge=0.5,
        le=30.0,
        alias="VEKTRA_ASSESS_ERASURE_WAIT_S",
        description=(
            "How long an erasure request waits for the worker before answering 503 "
            "ERR-ASSESS-027 pending (the same request again is idempotent)."
        ),
    )
    erasure_alert_after_s: int = Field(
        3600,
        # never below the purge's IN_FLIGHT (15 min, ``privacy.purge``; the unit test ties the
        # two): an erasure legitimately waits that long for a transcript copy that may be on the
        # wire, and alerting sooner would report a request working as designed
        ge=900,
        le=43_200,
        alias="VEKTRA_ASSESS_ERASURE_ALERT_AFTER_S",
        description=(
            "An erasure request still not completed this many seconds after it was made "
            "queues one operator_notify (erasure_overdue, OQ-033: the LMS cannot fail its own "
            "request). At least 900 s, the wait for a transcript copy in flight; at most 12 h: "
            "the daily purge records a request older than a day as failed (abandoned)."
        ),
    )
    db_backup_retention_days: int | None = Field(
        None,
        ge=0,
        le=3650,
        alias="VEKTRA_ASSESS_DB_BACKUP_RETENTION_DAYS",
        description=(
            "How long database backups are kept. Crypto-shredding covers a backup only once no "
            "retained backup still holds the wrapped key (ARCH-079 as amended by plan 12): "
            "unset, the worker warns at startup."
        ),
    )

    @field_validator("erasure_mode", "db_backup_retention_days", mode="before")
    @classmethod
    def _blank_privacy_is_unset(cls, value: Any) -> Any:
        """An empty value (compose's ``${VAR:-}``) is unset, never an invalid choice."""
        return None if isinstance(value, str) and not value.strip() else value

    # --- transcripts (plan assess-transcript-delivery) ---------------------------------------
    site_timezone: str = Field(
        "Europe/Rome",
        alias="VEKTRA_ASSESS_SITE_TIMEZONE",
        description=(
            "IANA time zone of the transcript's timestamps and of the date in its file name "
            "(BR-016: the date of CLOSED in the site time zone)."
        ),
    )
    download_token_ttl_s: int = Field(
        60,
        ge=5,
        le=120,
        alias="VEKTRA_ASSESS_DOWNLOAD_TOKEN_TTL_S",
        description=(
            "Lifetime of a transcript download token (aud=transcript, single use): at most "
            "120 s (ARCH-067)."
        ),
    )
    download_rpm: int = Field(
        10,
        ge=2,
        le=600,
        alias="VEKTRA_ASSESS_DOWNLOAD_RPM",
        description=(
            "Transcript operations per minute per transcript and requester, mints and downloads "
            "counted together (one download = 2 operations, so at least 2); 429 beyond it."
        ),
    )
    resend_min_interval_s: int = Field(
        600,
        ge=0,
        le=86_400,
        alias="VEKTRA_ASSESS_RESEND_MIN_INTERVAL_S",
        description=(
            "Minimum time between two delivery rounds of the same transcript (each round mails "
            "up to 3 graders): a resend sooner is 429 with Retry-After. Checked in the database "
            "under the per-transcript lock, so concurrent resends cannot both pass."
        ),
    )
    resend_rpm: int = Field(
        6,
        ge=1,
        le=600,
        alias="VEKTRA_ASSESS_RESEND_RPM",
        description="Resend calls per minute per API key (assess-local limiter); 429 beyond it.",
    )
    teacher_gate_max_age_s: int = Field(
        120,
        ge=10,
        le=3600,
        alias="VEKTRA_ASSESS_TEACHER_GATE_MAX_AGE_S",
        description=(
            "Freshness bound of the worker-written grader / editor lists a teacher request is "
            "checked against; older or absent -> 403 and a refresh is queued (fail closed)."
        ),
    )
    # --- teacher operations (plan assess-teacher-ops) -----------------------------------------
    rag_enabled: bool = Field(
        False,
        alias="VEKTRA_ASSESS_RAG_ENABLED",
        description=(
            "Course-material retrieval in discussions (ARCH-072). NOT AVAILABLE in this release: "
            "setting it to true is refused at startup, and a teacher's RAG ON is a field error. "
            "It exists only so that a deployment that sets it learns that at once."
        ),
    )
    reset_notice_enabled: bool = Field(
        False,
        alias="VEKTRA_ASSESS_RESET_NOTICE_ENABLED",
        description=(
            "REQ-092 student notice of a reset. OUT OF v1 (product owner 2026-11-08): setting it "
            "to true is refused at startup; no notice is queued or sent, and the teacher list "
            "shows 'studente non avvisato via email'."
        ),
    )

    @model_validator(mode="after")
    def _teacher_ops_refusals(self) -> Self:
        """Startup refusals (plan assess-teacher-ops): no dormant path for RAG nor for the
        REQ-092 reset notice (both not available in this release)."""
        if self.rag_enabled:
            raise ValueError(
                "VEKTRA_ASSESS_RAG_ENABLED=true is not available in this release: discussion "
                "retrieval stays off (ARCH-072). Unset it."
            )
        if self.reset_notice_enabled:
            raise ValueError(
                "VEKTRA_ASSESS_RESET_NOTICE_ENABLED=true is not available in this release: the "
                "REQ-092 reset notice is out of v1. Unset it."
            )
        return self

    # --- transcript delivery by SMTP (plan assess-transcript-delivery 10b; worker only) -------
    # The account is the worker-only secret ``assess_smtp`` ({"username", "password"}, ARCH-071
    # key matrix: the API refuses to start when it is mounted or named in its environment).
    smtp_host: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_SMTP_HOST",
        description="SMTP relay host of the transcript emails (worker only). Unset: delivery is not bound and the worker refuses to start unless VEKTRA_ASSESS_WORKER_ALLOW_UNBOUND is set (development).",
    )
    smtp_port: int = Field(
        587,
        ge=1,
        le=65535,
        alias="VEKTRA_ASSESS_SMTP_PORT",
        description="Relay port: 587 with STARTTLS, 465 with implicit TLS.",
    )
    smtp_tls_mode: Literal["starttls", "implicit"] = Field(
        "starttls",
        alias="VEKTRA_ASSESS_SMTP_TLS_MODE",
        description="'starttls' (STARTTLS required: a relay that does not offer it gets nothing) or 'implicit' (TLS from the first byte). There is no plaintext mode.",
    )
    smtp_from: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_SMTP_FROM",
        description="From address (and envelope sender) of the transcript emails.",
    )
    smtp_ca_file: Path | None = Field(
        None,
        alias="VEKTRA_ASSESS_SMTP_CA_FILE",
        description="CA bundle that verifies the relay's certificate (an on-prem relay with its own CA); the system store when unset. Certificates are always verified.",
    )
    smtp_max_connections: int = Field(
        2,
        ge=1,
        le=20,
        alias="VEKTRA_ASSESS_SMTP_MAX_CONNECTIONS",
        description="Concurrent sessions to the relay per worker.",
    )
    smtp_timeout_s: float = Field(
        30.0,
        ge=1,
        le=60,
        alias="VEKTRA_ASSESS_SMTP_TIMEOUT_S",
        description="Hard timeout of one send (connect, TLS, AUTH, DATA) or one probe.",
    )
    smtp_retry_schedule: str = Field(
        "1m,5m,15m,1h",
        alias="VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE",
        description="Waits after the 1st, 2nd, ... failed attempt (s, m, h); the last one repeats until the window closes.",
    )
    smtp_retry_window_hours: int = Field(
        72,
        ge=1,
        le=168,
        alias="VEKTRA_ASSESS_SMTP_RETRY_WINDOW_HOURS",
        description="Hours a delivery row is retried (from its creation) before it is FAILED (REQ-075: 24 to 72 h proposed).",
    )
    message_id_domain: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_MESSAGE_ID_DOMAIN",
        description="Right-hand side of the deterministic Message-ID; the domain of VEKTRA_ASSESS_SMTP_FROM when unset.",
    )
    recipient_domains: str = Field(
        "",
        alias="VEKTRA_ASSESS_RECIPIENT_DOMAINS",
        description="Comma list of the domains a transcript may be mailed to (exact match, REQ-082 as amended). Empty refuses every address: the delivery row is FAILED (domain_not_allowed) with an alert. The plugin validates the teacher's form against the same list.",
    )

    operator_email: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_OPERATOR_EMAIL",
        description="Address of the platform operator: the worker mails it the content-free operator alerts (REQ-071: repeated failed Starts, an LMS that cannot be read). Required with the relay: a production worker refuses to start without it. Not subject to VEKTRA_ASSESS_RECIPIENT_DOMAINS (it is deployment configuration, not a teacher's input).",
    )

    @field_validator("smtp_retry_schedule")
    @classmethod
    def _valid_schedule(cls, value: str) -> str:
        parse_schedule(value)  # raises ValueError: refused at startup, never replaced
        return value

    @field_validator(
        "smtp_host", "smtp_from", "message_id_domain", "operator_email", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("smtp_from")
    @classmethod
    def _bare_from(cls, value: str | None) -> str | None:
        """A bare addr-spec (no display name, no <>, no whitespace or CR/LF): it goes to
        the From header and to MAIL FROM as is."""
        if value is None:
            return None
        address = normalize_address(value)
        if address is None:
            raise ValueError(
                "VEKTRA_ASSESS_SMTP_FROM must be a bare e-mail address "
                "(name@domain, no display name, no <>, no spaces)"
            )
        return address

    @model_validator(mode="after")
    def _bare_operator(self) -> Self:
        """The same bare addr-spec rule as the From address, never repaired. Malformed:
        refused at startup while assess is enabled; while it is disabled only a warning, the
        value kept as given and never used (a dormant feature's setting never stops the API
        from starting, as for the UI origin)."""
        if self.operator_email is None:
            return self
        address = normalize_address(self.operator_email)
        if address is not None:
            # the settings are frozen: the normalised value replaces the raw one at load only
            object.__setattr__(self, "operator_email", address)
            return self
        if self.enabled:
            raise ValueError(
                "VEKTRA_ASSESS_OPERATOR_EMAIL must be a bare e-mail address "
                "(name@domain, no display name, no <>, no spaces)"
            )
        warnings.warn(
            "VEKTRA_ASSESS_OPERATOR_EMAIL is not a bare e-mail address; ignored while "
            "VEKTRA_ASSESS_ENABLED=false, refused once it is enabled",
            UserWarning,
            stacklevel=2,
        )
        return self

    @field_validator("smtp_ca_file", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    llm_allow_unactivated: bool = Field(
        False,
        alias="VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED",
        description=(
            "Development and CI only: serve discussions while the (provider, model, prompt_version) "
            "triple has no passing NFR-023 activation (without it the API starts degraded and the "
            "session pre-flight refuses discussions). Honoured only with VEKTRA_ENV=development; "
            "in any other environment the API refuses to start with it set."
        ),
    )


def erasure_mode_problem(settings: VektraAssessSettings) -> str | None:
    """Why the API must not start with these erasure settings (plan 12, OQ-033): no default
    for the DPO's decision outside development. ``None``: fine."""
    if (
        settings.enabled
        and settings.erasure_mode is None
        and not settings.is_development
    ):
        return (
            "VEKTRA_ASSESS_ERASURE_MODE must be set while assess is enabled outside development: "
            "'erase' or 'retain_exam_evidence' (the DPO's decision, OQ-033)"
        )
    return None


#: pydantic error types whose message names a rule and never the rejected input
def explain_settings_error(exc: Exception) -> tuple[str, str]:
    """``(detail, remediation)`` for a settings ``ValidationError``, naming each variable.

    One line per rejected ``VEKTRA_ASSESS_*`` variable with pydantic's reason (the bound it
    broke). The rejected VALUE is never echoed: the settings hold DSNs and the error goes
    to the startup log. The remediation names the variables to fix. The rule is shared with
    the LMS providers' settings (``vektra_shared.settings_errors``).
    """
    from vektra_shared.settings_errors import explain_settings_error as explain

    return explain(
        exc,
        VektraAssessSettings,
        fallback="the VEKTRA_ASSESS_* variables",
        docs="docs/reference/configuration.md",
    )
