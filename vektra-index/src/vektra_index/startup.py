"""Startup validation for vektra-index (ARCH-057 steps 5+6).

Step 5: Provider registration - verify EmbeddingProvider and VectorStoreProvider
        are registered in the ProviderRegistry.
Step 6: Embedding model warm-up - embed a test sentence and verify dimensionality.
"""

from __future__ import annotations

import logging

from vektra_shared.startup import StartupValidationError

logger = logging.getLogger(__name__)


async def check_provider_registration(registry) -> None:
    """ARCH-057 step 5: verify required providers are registered."""
    required = [
        ("embedding", "sentence-transformers"),
        ("vector_store", "pgvector"),
    ]
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


async def check_embedding_model(registry) -> None:
    """ARCH-057 step 6: warm up the embedding model and verify dimensionality."""
    try:
        embedding_provider = registry.get("embedding", "sentence-transformers")
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
