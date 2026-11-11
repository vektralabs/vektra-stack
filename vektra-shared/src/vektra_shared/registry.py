"""ProviderRegistry: generic dict-based registry for all pluggable providers (ARCH-039).

Usage:
    registry = ProviderRegistry()
    registry.register("embedding", "sentence-transformers", my_provider)
    provider = registry.get("embedding", "sentence-transformers")

Categories used by Vektra (ARCH-039, ARCH-057 step 5):
    "llm"               - LLMProvider implementations
    "embedding"         - EmbeddingProvider implementations
    "sparse_embedding"  - SparseEmbeddingProvider (Phase 2)
    "vector_store"      - VectorStoreProvider implementations
    "query_pipeline"    - QueryPipeline implementations
    "chunking"          - ChunkingStrategy implementations
    "extractor"         - DocumentExtractor implementations
    "safeguard"         - SafeguardHook implementations
    "event_emitter"     - EventEmitter implementations
    "key_store"         - KeyStoreProvider (for auth middleware)
    "lms"               - LmsProvider implementations (ADR-0033), loaded by entry point
"""

from __future__ import annotations

from typing import Any

#: same marker as ``vektra_testkit.recorders.TEST_DOUBLE_MARKER`` (not imported: the
#: registry is product code and the recorders are a test asset)
_TEST_DOUBLE_MARKER = "__vektra_test_double__"


class ProviderRegistry:
    """Dict-based registry for pluggable provider instances.

    All registered instances are keyed by (category, name). The registry
    validates existence on get() and raises ValueError with the list of
    available names when an unknown entry is requested.
    """

    def __init__(self, *, allow_test_doubles: bool = False) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        # The Recording* doubles live in vektra_testkit.recorders, a dev-only distribution that
        # the image never installs (plan task 3.1); a production registry still refuses them,
        # as defence in depth (ADR-0033 D8). Only a test that builds its own registry may opt in.
        self._allow_test_doubles = allow_test_doubles

    def register(self, category: str, name: str, instance: Any) -> None:
        """Register a provider instance under (category, name).

        Overwrites any existing registration for the same key without error. Refuses a
        recording test double unless the registry was built with ``allow_test_doubles``.
        """
        if not self._allow_test_doubles and getattr(
            type(instance), _TEST_DOUBLE_MARKER, False
        ):
            raise ValueError(
                f"refusing to register test double {type(instance).__name__} as "
                f"{category}/{name}: recorders are test assets"
            )
        if category not in self._store:
            self._store[category] = {}
        self._store[category][name] = instance

    def get(self, category: str, name: str) -> Any:
        """Retrieve a registered provider.

        Raises ValueError with available names if (category, name) is unknown.
        """
        category_map = self._store.get(category, {})
        if name not in category_map:
            available = list(category_map.keys())
            raise ValueError(
                f"No provider registered for category='{category}', name='{name}'. "
                f"Available: {available}"
            )
        return category_map[name]

    def list(self, category: str) -> list[str]:
        """Return all registered names for a category (empty list if category unknown)."""
        return list(self._store.get(category, {}).keys())

    def has(self, category: str, name: str) -> bool:
        """Return True if (category, name) is registered."""
        return name in self._store.get(category, {})
