"""The providers the app registers must be the ones startup validation requires.

BUG-024: `check_provider_registration` (ARCH-057 step 5) looks the sparse provider
up by its configured name (`fastembed-bm25`), but the app registered it only under
`"default"`, so every stack with `VEKTRA_SPARSE_EMBEDDING_PROVIDER` set died at
startup and hybrid search could not be enabled at all.

The existing check tests could not catch this: they hand `check_provider_registration`
a mock registry that already has the name, which asserts the check against a fiction.
These tests wire the *real* registration step to the *real* check, which is the only
place the two can be seen disagreeing.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vektra_app.main import _step_5_register_providers
from vektra_index.startup import check_provider_registration
from vektra_shared.config import VektraSettings
from vektra_shared.registry import ProviderRegistry

# FastEmbedBM25Provider loads its model in __init__, and registration reaches the
# database from the key store onwards. Those are the only things stubbed here;
# the registration itself runs exactly as it does in production.
_SPARSE_PROVIDER = "vektra_index.providers.fastembed_bm25.FastEmbedBM25Provider"
_SESSION_FACTORY = "vektra_shared.db.get_session_factory"
_KEY_STORE_LOAD = "vektra_admin.keystore.InMemoryKeyStore.load_from_db"


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run these tests against the settings they declare, not the machine's .env.

    Two leaks, and both have to be closed (same defect class as DEBT-025):

    - `os.environ`: importing litellm, which registration does, runs `load_dotenv()`
      and pulls the repo's .env into the process environment for the rest of the
      session. `_env_file=None` does not help, because BaseSettings still reads
      `os.environ`.
    - the .env **file** itself: registration builds sub-configs of its own
      (`QueryPipelineConfig()`), which resolve `env_file=".env"` relative to the
      working directory and so bypass the settings passed in. Running from an
      empty cwd is what actually closes this one.

    Without both, a field left at its default silently picks up local config: on a
    developer machine with VEKTRA_RERANK_ENABLED=true, registration went off and
    loaded a cross-encoder that CI, which has no .env, never loaded. The test then
    exercises a different code path on every machine.
    """
    for key in [k for k in os.environ if k.startswith("VEKTRA_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    # Reranking is on by default (RerankConfig.enabled), and registration reads that
    # config itself rather than the settings passed in, so it would load a
    # cross-encoder on every test. Nothing here asserts on the reranker; pin it off
    # so these tests do not depend on a model being downloadable.
    monkeypatch.setenv("VEKTRA_RERANK_ENABLED", "false")


def _settings(**overrides: object) -> VektraSettings:
    """Settings built in isolation from the machine's .env (see _scrub_vektra_env)."""
    params: dict[str, object] = {
        "llm_provider": "ollama/llama3",
        "vector_store_provider": "pgvector",
        "sparse_embedding_provider": None,
    }
    params.update(overrides)
    return VektraSettings(_env_file=None, **params)


def _fake_session_factory() -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


async def _register(settings: VektraSettings) -> ProviderRegistry:
    registry = ProviderRegistry()
    with (
        patch(_SPARSE_PROVIDER, return_value=MagicMock()),
        patch(_SESSION_FACTORY, _fake_session_factory),
        patch(_KEY_STORE_LOAD, AsyncMock()),
    ):
        await _step_5_register_providers(settings, registry)
    return registry


@pytest.mark.asyncio
async def test_sparse_provider_is_registered_under_its_configured_name() -> None:
    """The name the check asks for is the name the app registers."""
    registry = await _register(_settings(sparse_embedding_provider="fastembed-bm25"))

    # "default" is what callers resolve; the configured name is what the startup
    # check resolves. Both must be present, as they already are for the vector store.
    assert registry.has("sparse_embedding", "default")
    assert registry.has("sparse_embedding", "fastembed-bm25")


@pytest.mark.asyncio
async def test_startup_check_passes_against_the_real_registry() -> None:
    """The assertion that matters: the stack would actually boot with sparse on."""
    settings = _settings(sparse_embedding_provider="fastembed-bm25")
    registry = await _register(settings)

    # Raises StartupValidationError if registration and validation disagree
    await check_provider_registration(
        registry,
        vector_store_provider=settings.vector_store_provider,
        sparse_embedding_provider=settings.sparse_embedding_provider,
    )


@pytest.mark.asyncio
async def test_startup_check_passes_with_sparse_disabled() -> None:
    """The default path, with no sparse provider configured, keeps working."""
    settings = _settings()
    registry = await _register(settings)

    assert not registry.has("sparse_embedding", "default")

    await check_provider_registration(
        registry,
        vector_store_provider=settings.vector_store_provider,
        sparse_embedding_provider=settings.sparse_embedding_provider,
    )
