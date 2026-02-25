"""Unit tests for vektra_index.startup (ARCH-057 steps 5-6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from vektra_shared.startup import StartupValidationError


def _make_registry(
    has_embedding: bool = True,
    has_vector_store: bool = True,
    embed_dims: int = 384,
    reported_dims: int = 384,
) -> MagicMock:
    """Build a mock ProviderRegistry for startup checks."""
    registry = MagicMock()

    def _has(category: str, name: str) -> bool:
        if category == "embedding":
            return has_embedding
        if category == "vector_store":
            return has_vector_store
        return False

    registry.has.side_effect = _has
    registry.list.return_value = []

    embedding_provider = MagicMock()
    embedding_provider.embed_query = AsyncMock(return_value=[0.1] * embed_dims)
    embedding_provider.dimensions.return_value = reported_dims
    registry.get.return_value = embedding_provider

    return registry


@pytest.mark.asyncio
async def test_check_provider_registration_happy_path() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry()
    await check_provider_registration(registry)  # should not raise


@pytest.mark.asyncio
async def test_check_provider_registration_missing_embedding() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_embedding=False)
    with pytest.raises(StartupValidationError, match="embedding"):
        await check_provider_registration(registry)


@pytest.mark.asyncio
async def test_check_provider_registration_missing_vector_store() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_vector_store=True, has_embedding=True)
    # Override just vector_store
    original_has = registry.has.side_effect

    def _has_no_vs(cat: str, name: str) -> bool:
        if cat == "vector_store":
            return False
        return original_has(cat, name)

    registry.has.side_effect = _has_no_vs

    with pytest.raises(StartupValidationError, match="vector_store"):
        await check_provider_registration(registry)


@pytest.mark.asyncio
async def test_check_embedding_model_happy_path() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry(embed_dims=384, reported_dims=384)
    await check_embedding_model(registry)  # should not raise


@pytest.mark.asyncio
async def test_check_embedding_model_dimension_mismatch() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry(embed_dims=384, reported_dims=768)
    with pytest.raises(StartupValidationError, match="dimensions"):
        await check_embedding_model(registry)


@pytest.mark.asyncio
async def test_check_embedding_model_embed_failure() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry()
    provider = registry.get.return_value
    provider.embed_query.side_effect = RuntimeError("Model not found")

    with pytest.raises(StartupValidationError, match="Model not found"):
        await check_embedding_model(registry)
