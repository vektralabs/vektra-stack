"""Unit tests for vektra_index.startup (ARCH-057 steps 5-6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from vektra_shared.startup import StartupValidationError
from vektra_shared.types import HealthStatus, SparseVector


def _make_registry(
    has_embedding: bool = True,
    has_vector_store: bool = True,
    has_sparse: bool = False,
    embed_dims: int = 384,
    reported_dims: int = 384,
    vector_store_name: str = "pgvector",
) -> MagicMock:
    """Build a mock ProviderRegistry for startup checks."""
    registry = MagicMock()

    def _has(category: str, name: str) -> bool:
        if category == "embedding":
            return has_embedding
        if category == "vector_store":
            return has_vector_store and name == vector_store_name
        if category == "sparse_embedding":
            return has_sparse
        return False

    registry.has.side_effect = _has
    registry.list.return_value = []

    embedding_provider = MagicMock()
    embedding_provider.embed_query = AsyncMock(return_value=[0.1] * embed_dims)
    embedding_provider.dimensions.return_value = reported_dims

    qdrant_provider = MagicMock()
    qdrant_provider.health_check = AsyncMock(
        return_value=HealthStatus(status="healthy", latency_ms=5)
    )
    qdrant_provider.ensure_collection = AsyncMock()

    sparse_provider = MagicMock()
    sparse_provider.embed_query = AsyncMock(
        return_value=SparseVector(indices=[1, 5], values=[0.3, 0.7])
    )

    def _get(category: str, name: str) -> MagicMock:
        if category == "embedding":
            return embedding_provider
        if category == "vector_store" and name == "qdrant":
            return qdrant_provider
        if category == "sparse_embedding":
            return sparse_provider
        return MagicMock()

    registry.get.side_effect = _get

    return registry


# --- check_provider_registration ---


async def test_check_provider_registration_happy_path() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry()
    await check_provider_registration(registry)  # should not raise


async def test_check_provider_registration_missing_embedding() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_embedding=False)
    with pytest.raises(StartupValidationError, match="embedding"):
        await check_provider_registration(registry)


async def test_check_provider_registration_missing_vector_store() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_vector_store=False)
    with pytest.raises(StartupValidationError, match="vector_store"):
        await check_provider_registration(registry)


async def test_check_provider_registration_qdrant() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_vector_store=True, vector_store_name="qdrant")
    await check_provider_registration(registry, vector_store_provider="qdrant")


async def test_check_provider_registration_with_sparse() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_sparse=True)
    await check_provider_registration(
        registry, sparse_embedding_provider="fastembed-bm25"
    )


async def test_check_provider_registration_missing_sparse() -> None:
    from vektra_index.startup import check_provider_registration

    registry = _make_registry(has_sparse=False)
    with pytest.raises(StartupValidationError, match="sparse_embedding"):
        await check_provider_registration(
            registry, sparse_embedding_provider="fastembed-bm25"
        )


# --- check_embedding_model ---


async def test_check_embedding_model_happy_path() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry(embed_dims=384, reported_dims=384)
    await check_embedding_model(registry)  # should not raise


async def test_check_embedding_model_dimension_mismatch() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry(embed_dims=384, reported_dims=768)
    with pytest.raises(StartupValidationError, match="dimensions"):
        await check_embedding_model(registry)


async def test_check_embedding_model_embed_failure() -> None:
    from vektra_index.startup import check_embedding_model

    registry = _make_registry()
    provider = registry.get("embedding", "sentence-transformers")
    provider.embed_query.side_effect = RuntimeError("Model not found")

    with pytest.raises(StartupValidationError, match="Model not found"):
        await check_embedding_model(registry)


# --- check_qdrant_connectivity ---


async def test_check_qdrant_connectivity_happy_path() -> None:
    from vektra_index.startup import check_qdrant_connectivity

    registry = _make_registry(vector_store_name="qdrant")
    await check_qdrant_connectivity(registry)


async def test_check_qdrant_connectivity_unhealthy() -> None:
    from vektra_index.startup import check_qdrant_connectivity

    registry = _make_registry(vector_store_name="qdrant")
    qdrant_provider = registry.get("vector_store", "qdrant")
    qdrant_provider.health_check = AsyncMock(
        return_value=HealthStatus(status="unhealthy", message="connection refused")
    )

    with pytest.raises(StartupValidationError, match="unhealthy"):
        await check_qdrant_connectivity(registry)


async def test_check_qdrant_connectivity_exception() -> None:
    from vektra_index.startup import check_qdrant_connectivity

    registry = _make_registry(vector_store_name="qdrant")
    qdrant_provider = registry.get("vector_store", "qdrant")
    qdrant_provider.health_check = AsyncMock(side_effect=ConnectionError("refused"))

    with pytest.raises(StartupValidationError, match="refused"):
        await check_qdrant_connectivity(registry)


# --- check_sparse_embedding_model ---


async def test_check_sparse_embedding_model_happy_path() -> None:
    from vektra_index.startup import check_sparse_embedding_model

    registry = _make_registry(has_sparse=True)
    await check_sparse_embedding_model(registry)


async def test_check_sparse_embedding_model_failure() -> None:
    from vektra_index.startup import check_sparse_embedding_model

    registry = _make_registry(has_sparse=True)
    sparse_provider = registry.get("sparse_embedding", "default")
    sparse_provider.embed_query = AsyncMock(
        side_effect=ImportError("fastembed not found")
    )

    with pytest.raises(StartupValidationError, match="fastembed"):
        await check_sparse_embedding_model(registry)
