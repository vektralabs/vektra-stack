"""Startup validation for vektra-index (ARCH-057 steps 5+6).

Step 5: Provider registration - verify EmbeddingProvider and VectorStoreProvider
        are registered in the ProviderRegistry.
Step 6: Embedding model warm-up - embed a test sentence and verify dimensionality.

Phase 2 additions:
  - Qdrant connectivity check (when vector_store_provider=qdrant)
  - Sparse embedding model load check (when sparse_embedding_provider is set)
"""

from __future__ import annotations

import logging
from typing import Any

from vektra_shared.startup import StartupValidationError

logger = logging.getLogger(__name__)


async def check_provider_registration(
    registry: Any,
    vector_store_provider: str = "pgvector",
    sparse_embedding_provider: str | None = None,
) -> None:
    """ARCH-057 step 5: verify required providers are registered.

    Checks the configured vector store provider name (pgvector or qdrant)
    and optionally the sparse embedding provider.
    """
    required = [
        ("embedding", "sentence-transformers"),
        ("vector_store", vector_store_provider),
    ]
    if sparse_embedding_provider:
        required.append(("sparse_embedding", sparse_embedding_provider))

    for category, name in required:
        if not registry.has(category, name):
            available = registry.list(category)
            raise StartupValidationError(
                step=f"provider_registration:{category}",
                detail=f"Provider '{name}' not registered in category '{category}'. Available: {available}",
                remediation=(
                    f"Ensure the '{name}' provider is configured via VEKTRA_{category.upper()}_PROVIDER "
                    f"and registered at application startup."
                ),
            )


async def check_embedding_model(registry: Any) -> None:
    """ARCH-057 step 6: warm up the embedding model and verify dimensionality."""
    try:
        # "default" aliases whichever provider is active
        # (sentence-transformers or tei, FEAT-024).
        embedding_provider = registry.get("embedding", "default")
        test_embedding = await embedding_provider.embed_query("startup validation test")
        actual_dims = len(test_embedding)
        expected_dims = embedding_provider.dimensions()

        if actual_dims != expected_dims:
            raise StartupValidationError(
                step="embedding_model",
                detail=(
                    f"Embedding model returned {actual_dims} dimensions "
                    f"but dimensions() reports {expected_dims}."
                ),
                remediation="Check VEKTRA_EMBEDDING_MODEL configuration.",
            )
        logger.info("Embedding model warm-up OK: %d dimensions", actual_dims)

    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step="embedding_model",
            detail=str(exc),
            remediation=(
                "Check that the embedding model files are accessible. "
                "Set VEKTRA_EMBEDDING_MODEL to a valid sentence-transformers model name."
            ),
        ) from exc


async def check_qdrant_connectivity(registry: Any) -> None:
    """Verify Qdrant connectivity when vector_store_provider=qdrant.

    Calls health_check() on the registered Qdrant provider and ensures
    the collection exists (or creates it).
    """
    try:
        provider = registry.get("vector_store", "qdrant")
        status = await provider.health_check()
        if status.status != "healthy":
            raise StartupValidationError(
                step="qdrant_connectivity",
                detail=f"Qdrant health check returned: {status.status} - {status.message}",
                remediation=(
                    "Ensure Qdrant is running at VEKTRA_QDRANT_URL. "
                    "Start it with: docker compose --profile qdrant up -d"
                ),
            )
        # Ensure collection exists
        if hasattr(provider, "ensure_collection"):
            await provider.ensure_collection()
        logger.info("Qdrant connectivity OK (latency: %dms)", status.latency_ms or 0)
    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step="qdrant_connectivity",
            detail=str(exc),
            remediation=(
                "Check VEKTRA_QDRANT_URL and VEKTRA_QDRANT_API_KEY configuration. "
                "Ensure Qdrant is running and accessible."
            ),
        ) from exc


async def check_sparse_embedding_model(registry: Any) -> None:
    """Verify the sparse embedding model loads successfully.

    Called when VEKTRA_SPARSE_EMBEDDING_PROVIDER is set.
    """
    try:
        # "default" is the ProviderRegistry convention for runtime lookup;
        # the configured provider name is only used during registration.
        provider = registry.get("sparse_embedding", "default")
        test_result = await provider.embed_query("startup validation test")
        if not test_result.indices or not test_result.values:
            logger.warning("Sparse embedding returned empty vector for test input")
        else:
            logger.info(
                "Sparse embedding model OK: %d non-zero entries",
                len(test_result.indices),
            )
    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step="sparse_embedding_model",
            detail=str(exc),
            remediation=(
                "Check VEKTRA_SPARSE_EMBEDDING_PROVIDER and VEKTRA_SPARSE_EMBEDDING_MODEL. "
                "Ensure fastembed is installed: pip install 'vektra-index[sparse]'"
            ),
        ) from exc
