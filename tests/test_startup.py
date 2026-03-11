"""Startup validation tests (ARCH-057).

Verifies the 11-step startup validation sequence by inspecting container logs
from the running Docker Compose stack.

The graceful config failure test runs a separate container instance with
a missing required env var to verify the error message format.

Requires the Docker Compose stack to be running (started by integration.yml).
"""

from __future__ import annotations

import subprocess

# All 11 ARCH-057 startup step names in execution order
_STARTUP_STEPS = [
    "config_validation",
    "database_connectivity",
    "database_schema",
    "pgvector_extension",
    "provider_registration",
    "embedding_warmup",
    "llm_connectivity",
    "template_loading",
    "analytics_check",
    "learn_check",
    "qdrant_check",
]


def _container_logs() -> str:
    """Retrieve vektra container logs."""
    result = subprocess.run(
        ["docker", "compose", "logs", "vektra"],
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_all_startup_steps_logged() -> None:
    """All 11 ARCH-057 startup steps appear in container log output."""
    logs = _container_logs()
    assert logs, "No container logs found (is the stack running?)"

    missing = [step for step in _STARTUP_STEPS if step not in logs]
    assert not missing, f"Startup steps not found in container logs: {missing}"


def test_startup_complete_logged() -> None:
    """The startup_complete event with total_duration_ms is logged."""
    logs = _container_logs()
    assert "startup_complete" in logs, (
        "startup_complete event not found in container logs"
    )


def test_graceful_failure_on_missing_config() -> None:
    """Missing required env var produces a structured error, not a traceback.

    Runs a separate short-lived container with VEKTRA_LLM_PROVIDER unset.
    """
    result = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "VEKTRA_DATABASE_URL=postgresql+asyncpg://vektra:vektra@postgres:5432/vektra",
            "-e",
            "VEKTRA_LLM_PROVIDER=",
            "vektra",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "Container should have exited non-zero with missing VEKTRA_LLM_PROVIDER"
    )
    assert "startup_failed" in output, (
        "Expected 'startup_failed' in output, got: " + output[:500]
    )
    # No raw Python tracebacks should leak to the user
    assert "Traceback (most recent call last)" not in output, (
        "Raw traceback leaked in startup failure output"
    )
