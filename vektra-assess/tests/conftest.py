"""Test environment isolation for vektra-assess (DEBT-029).

The fixture is autouse and shared: importing it here registers it for every test in
this package. See `vektra_shared.testing` for why a scrub alone is not enough.
"""

from __future__ import annotations

from vektra_shared.testing import hermetic_env  # noqa: F401  (autouse fixture)
