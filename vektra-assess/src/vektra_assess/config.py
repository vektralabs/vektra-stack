"""Assess settings (VektraAssessSettings).

Every assess setting is read here, with an explicit ``VEKTRA_ASSESS_*`` alias and no
env_file: the assess processes read their environment and their secret files, nothing
else. Later plans add their fields to this class only. Secrets never live here: they are
keyring files under ``SECRETS_DIR`` (``vektra_assess.keys``).

Owning plan: .s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # --- Moodle web-service client (plan assess-moodle-ws, group B) -------------------------
    env: str = Field(
        "development",
        alias="VEKTRA_ENV",
        description="Environment mode (shared with the main settings). Plain http to Moodle needs development.",
    )
    moodle_ca_file: Path | None = Field(
        None,
        alias="VEKTRA_ASSESS_MOODLE_CA_FILE",
        description="CA bundle used to verify Moodle TLS certificates; the system store when unset.",
    )
    ws_timeout_connect_s: float = Field(
        3.0, gt=0, le=60, alias="VEKTRA_ASSESS_WS_TIMEOUT_CONNECT_S"
    )
    ws_timeout_read_s: float = Field(
        10.0, gt=0, le=120, alias="VEKTRA_ASSESS_WS_TIMEOUT_READ_S"
    )
    ws_total_timeout_s: float = Field(
        30.0,
        gt=0,
        le=600,
        alias="VEKTRA_ASSESS_WS_TOTAL_TIMEOUT_S",
        description="Overall deadline of one Moodle call, body included: a server that drips bytes cannot hold it open.",
    )
    ws_max_response_bytes: int = Field(
        8 * 1024 * 1024,
        ge=1024,
        alias="VEKTRA_ASSESS_WS_MAX_RESPONSE_BYTES",
        description="Byte cap of a JSON web-service response; the stream is aborted on overflow.",
    )
    ws_max_file_bytes: int = Field(
        10 * 1024 * 1024,
        ge=1024,
        alias="VEKTRA_ASSESS_WS_MAX_FILE_BYTES",
        description="Byte cap of one downloaded submission file (REQ-101).",
    )
    ws_rps: float = Field(
        10.0,
        gt=0,
        le=1000,
        alias="VEKTRA_ASSESS_WS_RPS",
        description="Global cap on Moodle calls per second, shared by every caller in the worker.",
    )
    breaker_threshold: int = Field(5, ge=1, alias="VEKTRA_ASSESS_BREAKER_THRESHOLD")
    breaker_cooldown_s: float = Field(
        30.0, gt=0, alias="VEKTRA_ASSESS_BREAKER_COOLDOWN_S"
    )
    moodle_allow_http: bool = Field(
        False,
        alias="VEKTRA_ASSESS_MOODLE_ALLOW_HTTP",
        description="Plain http to Moodle. Honoured only with VEKTRA_ENV=development; the worker refuses to start otherwise.",
    )
    moodle_allowed_cidrs: str = Field(
        "",
        alias="VEKTRA_ASSESS_MOODLE_ALLOWED_CIDRS",
        description="Comma-separated CIDRs a Moodle host may resolve into (an on-prem Moodle on a private network). Empty: private addresses are refused.",
    )
    ingest_account_username: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME",
        description="Username of the ingestion web-service account; the setup check refuses the assess token if it belongs to it. Unset: that item is UNVERIFIABLE.",
    )
    ui_origin: str | None = Field(
        None,
        alias="VEKTRA_ASSESS_UI_ORIGIN",
        description="Origin the chat UI is served from; the setup check refuses it if it is the same site as the Moodle base_url or the API public origin (CONF-004). Unset: UNVERIFIABLE.",
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
        None, ge=0, alias="VEKTRA_ASSESS_LLM_PRICE_PER_1K_IN"
    )
    llm_price_per_1k_out: float | None = Field(
        None, ge=0, alias="VEKTRA_ASSESS_LLM_PRICE_PER_1K_OUT"
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
        description="Where the discussion model runs. 'cloud' (the safe default) makes the eval adapter refuse any dataset whose data_basis is not 'synthetic' before a single request (NFR-022 data basis); set 'onprem' only for a model on the institution's own hardware.",
    )
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
    retry_rpm: int = Field(
        6,
        ge=1,
        le=600,
        alias="VEKTRA_ASSESS_RETRY_RPM",
        description="POST /sessions/{sid}/retry calls per minute per session (REQ-086).",
    )
    outage_min_credit_s: int = Field(
        20,
        ge=0,
        le=300,
        alias="VEKTRA_ASSESS_OUTAGE_MIN_CREDIT_S",
        description=(
            "REQ-086 G: an outage shorter than this earns no time back; a longer one is credited "
            "in full, the cumulative credit capped at C = min(15 min, half the duration)."
        ),
    )
    max_message_chars: int = Field(
        2000,
        ge=1,
        le=20_000,
        alias="VEKTRA_ASSESS_MAX_MESSAGE_CHARS",
        description=(
            "Longest student message accepted (REQ-083); longer or blank is a 400 before any model "
            "call. PROVISIONAL default: plan M5.1 sizes it on the reference session."
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
