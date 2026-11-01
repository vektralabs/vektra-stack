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
        ge=5,
        alias="VEKTRA_ASSESS_HEARTBEAT_MAX_AGE_S",
        description="A worker heartbeat older than this makes /health/assess non-healthy.",
    )
    worker_heartbeat_file: Path = Field(
        Path("/tmp/vektra-assess-worker.heartbeat"),
        alias="VEKTRA_ASSESS_WORKER_HEARTBEAT_FILE",
        description="File whose mtime the worker refreshes every 10 s (container healthcheck).",
    )
    parser_socket: Path = Field(
        Path("/run/vektra-assess-parser/parser.sock"),
        alias="VEKTRA_ASSESS_PARSER_SOCKET",
        description="Unix socket of the parser sidecar (worker and parser only).",
    )
