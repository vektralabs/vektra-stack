"""Test environment isolation for vektra-assess (DEBT-029).

The fixture is autouse and shared: importing it here registers it for every test in
this package. See `vektra_shared.testing` for why a scrub alone is not enough.
"""

from __future__ import annotations

import pytest
import structlog

from vektra_shared.testing import hermetic_env  # noqa: F401  (autouse fixture)


@pytest.fixture(autouse=True)
def _fresh_structlog():
    """A structlog logger caches ``sys.stdout`` when it is first used. A test that ran under
    output capture leaves a closed file behind, and the next test that logs through the same
    module-level logger then dies with "I/O operation on closed file". Start every test from
    the default configuration so each one binds the stream that is current."""
    structlog.reset_defaults()
    yield
