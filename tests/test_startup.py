"""Startup validation tests (ARCH-057).

Verifies the 11-step startup validation sequence by inspecting container logs
from the running Docker Compose stack.

The graceful config failure test runs a separate container instance with
a missing required env var to verify the error message format.

Requires the Docker Compose stack to be running (started by integration.yml).
"""

from __future__ import annotations

import subprocess

import pytest

# Needs the running stack, so it is an integration test and now says so. Unmarked, it
# was indistinguishable from a unit test that `make test` had merely forgotten — and
# in fact no runner ran it at all, despite the docstring above assuming otherwise
# (DEBT-031).
pytestmark = pytest.mark.integration

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

    cursor = 0
    for step in _STARTUP_STEPS:
        idx = logs.find(step, cursor)
        assert idx != -1, f"Startup step not found in order in container logs: {step}"
        cursor = idx + len(step)


def test_startup_complete_logged() -> None:
    """The startup_complete event with total_duration_ms is logged."""
    logs = _container_logs()
    assert "startup_complete" in logs, (
        "startup_complete event not found in container logs"
    )


def _run_with_empty_llm_provider() -> str:
    """Boot a throwaway container whose VEKTRA_LLM_PROVIDER is the empty string.

    `-e VEKTRA_LLM_PROVIDER=` sets the variable to `""`; it does not unset it, as this
    module used to claim. That mattered: `llm_provider` was a bare required `str`, `""`
    is a valid `str`, so nothing raised, the container booted and served, and the
    misconfiguration surfaced at the first query instead of at startup. The test never
    once ran (DEBT-031), so nobody found out. The field is now `min_length=1`, and an
    empty value fails at step 1 exactly as an absent one does.
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
    assert result.returncode != 0, (
        "Container should have exited non-zero with an empty VEKTRA_LLM_PROVIDER"
    )
    return result.stdout + result.stderr


def test_graceful_failure_on_missing_config() -> None:
    """A required env var with no usable value fails startup, not the first query."""
    output = _run_with_empty_llm_provider()
    assert "startup_failed" in output, (
        "Expected 'startup_failed' in output, got: " + output[:500]
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG-025: the structured [STARTUP ERROR] block is emitted, but `raise "
        "SystemExit(1)` then travels out of the ASGI lifespan into uvicorn, which logs "
        "the exception — so the operator gets the good message and a Python traceback. "
        "NFR-009 asks for the former instead of the latter, not both. Fixing it means "
        "validating before uvicorn.run() rather than inside the lifespan, which is the "
        "boot path BUG-024 broke, so it is tracked separately. strict=True: when "
        "BUG-025 lands this test XPASSes and the suite goes red until the marker goes."
    ),
)
def test_no_raw_traceback_on_startup_failure() -> None:
    """A misconfiguration is reported, not dumped as a stack trace (REQ-011, NFR-009)."""
    output = _run_with_empty_llm_provider()
    assert "Traceback (most recent call last)" not in output, (
        "Raw traceback leaked in startup failure output"
    )
