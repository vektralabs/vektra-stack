"""Test-support helpers shared by every package's test suite (DEBT-029).

Imported by each `vektra-*/tests/conftest.py`, never by product code.

## The leak this closes

`litellm` calls `dotenv.load_dotenv()` at **import time** (guarded only by
`LITELLM_MODE`), and it finds the repo's `.env` by walking up from its own module
path inside `.venv/` — which lives inside the repo. So the developer's `.env` lands
in `os.environ` and stays there for the rest of the pytest session.

DEBT-025 tried to fix this with an autouse scrub. That cannot work on its own, and
the reason is worth stating because it is not obvious: the scrub runs **before the
test body**, while the import that re-injects the `.env` happens **inside** it (the
product imports litellm lazily). Any test whose code path reaches litellm gets the
local `.env` back, scrub or no scrub. Measured on 2026-07-14: with the scrub active,
`VectorStoreConfig().vector_store_provider` still resolved to `qdrant` (the value in
a developer's `.env`) instead of `pgvector` (the default) in **all four** packages
tested, including the three that had the fixture.

Setting `LITELLM_MODE` before litellm is imported is what actually closes it: litellm
then skips the `load_dotenv()` call entirely. This module does that at import, and
conftest modules are imported before the test modules that pull in litellm.

Changing the working directory does **not** help, and an earlier draft of DEBT-029
wrongly claimed it would: `load_dotenv()` resolves the file relative to the calling
module, not the cwd. Nor do the settings classes read the `.env` file themselves —
they have no `env_file` in their `SettingsConfigDict` and only ever read `os.environ`.
"""

from __future__ import annotations

import os

import pytest

# External provider keys read by ExternalApiKeys (no VEKTRA_ prefix).
_EXTERNAL_API_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

# Must be set before litellm is imported anywhere: it reads this at import time and
# skips its dotenv load when the value is not "DEV". This module is imported from
# conftest, which pytest loads before any test module, so the ordering holds.
os.environ["LITELLM_MODE"] = "PRODUCTION"


@pytest.fixture(autouse=True)
def hermetic_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scrub ambient config so a default asserted in a test is the real default.

    Complements the `LITELLM_MODE` guard above: that stops the `.env` from being
    injected, this removes anything the developer exported in their shell.

    Integration tests are exempt. They talk to a real stack on purpose and set the
    variables they need (database URL, bootstrap key) themselves; scrubbing under
    them would take away the environment they are testing against.
    """
    if request.node.get_closest_marker("integration"):
        return

    for name in list(os.environ):
        if name.startswith("VEKTRA_") or name in _EXTERNAL_API_KEYS:
            monkeypatch.delenv(name)
