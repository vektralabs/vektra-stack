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


def _settings(**overrides: object) -> VektraSettings:
    """Settings built in isolation from the machine's .env.

    Every field these tests depend on is passed explicitly, `_env_file=None`
    included: importing litellm (which registration does) runs `load_dotenv()`,
    which pulls the repo's .env into `os.environ` for the rest of the session.
    A setting left to its default would then silently pick up the developer's
    local configuration, and the sparse-disabled case would stop testing what
    its name says.
    """
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
