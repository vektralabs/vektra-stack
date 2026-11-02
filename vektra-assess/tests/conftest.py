"""Test environment isolation for vektra-assess (DEBT-029).

The fixture is autouse and shared: importing it here registers it for every test in
this package. See `vektra_shared.testing` for why a scrub alone is not enough.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import structlog

from vektra_shared.testing import hermetic_env  # noqa: F401  (autouse fixture)

# Test modules import helper modules that sit beside them (assess_moodle_support, the reader
# tests reused by the live tests). Under `pytest vektra-*/tests` with --import-mode=importlib
# this directory is not on sys.path, so put it there once, for every invocation style.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _fresh_structlog():
    """A structlog logger caches ``sys.stdout`` when it is first used. A test that ran under
    output capture leaves a closed file behind, and the next test that logs through the same
    module-level logger then dies with "I/O operation on closed file". Start every test from
    the default configuration so each one binds the stream that is current."""
    structlog.reset_defaults()
    yield
