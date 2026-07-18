"""The local .env must not reach a test's view of the config (DEBT-029).

This lives in vektra-core because litellm is its dependency, and litellm is the
carrier: it calls `dotenv.load_dotenv()` at import time and finds the repo's `.env`
by walking up from its own module inside `.venv/`. Putting the test in a package
where litellm is not installed would make it skip in CI, which is the failure mode
this whole debt is about.

Before the fix, this test failed on any machine with a populated `.env` and passed in
CI — the two ran different code paths, which is exactly what a test is supposed to
rule out.
"""

from __future__ import annotations

import os


def test_importing_litellm_does_not_inject_the_local_env() -> None:
    """litellm's import-time load_dotenv() must be disarmed, not merely undone.

    An autouse scrub cannot close this: it runs before the test body, while the
    import that re-injects the .env happens inside it. The guard is `LITELLM_MODE`,
    set by vektra_shared.testing before any test module is loaded.
    """
    assert os.environ.get("LITELLM_MODE") == "PRODUCTION"

    import litellm  # noqa: F401  (the import is the point)

    leaked = [name for name in os.environ if name.startswith("VEKTRA_")]
    assert leaked == [], (
        f"importing litellm re-injected the local .env: {leaked}. "
        "The scrub cannot help here; LITELLM_MODE must be set before the import."
    )


def test_a_config_left_at_its_default_resolves_to_the_default() -> None:
    """The canary: the default is 'pgvector', a developer's .env says 'qdrant'.

    Reading 'qdrant' here means the test is running against local configuration
    rather than the code's own defaults.
    """
    import litellm  # noqa: F401  (as the product does, lazily, mid-test)

    from vektra_shared.config import VectorStoreConfig

    assert VectorStoreConfig().vector_store_provider == "pgvector"
