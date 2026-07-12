"""Shared fixtures for vektra-core unit tests."""

from __future__ import annotations

import os

import pytest

# External provider keys read by ExternalApiKeys (no VEKTRA_ prefix).
_EXTERNAL_API_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub ambient config vars so default assertions stay hermetic (DEBT-025).

    When the full suite runs on a dev machine, imports during collection
    (litellm calls dotenv.load_dotenv) leak the local .env into os.environ,
    overriding config defaults (e.g. VEKTRA_PARENT_EXPANSION_ENABLED,
    VEKTRA_EVAL_MODE). CI has no .env and never sees the difference. Tests
    that need a specific value still set it explicitly via constructor
    kwargs or monkeypatch.setenv.
    """
    for name in list(os.environ):
        if name.startswith("VEKTRA_") or name in _EXTERNAL_API_KEYS:
            monkeypatch.delenv(name)
