"""Assess settings (VektraAssessSettings).

Every assess setting is read here, with an explicit ``VEKTRA_ASSESS_*`` alias and no
env_file: the assess processes read their environment and their secret files, nothing
else. Later plans add their fields to this class only. Secrets never live here: they are
keyring files under ``SECRETS_DIR`` (``vektra_assess.keys``).

Owning plan: .s2s/plans/20261101-154950-assess-foundations.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
