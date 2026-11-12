"""Test environment of the Moodle provider (ADR-0033 D10).

The provider's own tests need only ``vektra_shared`` and the recordings. The joint suites (the
vertical's capture, Part A, canary sinks and setup check over the recorded responses, ADR-0033
D4) also need the vertical's test harness: its in-process parser, its real-Postgres fixture, its
world and pre-flight support. They run with the platform installed beside the provider (the
``test-lms-moodle`` CI job, ``make test-lms-moodle``), and this conftest makes the vertical's test
directory importable and takes over its fixtures, so both suites run in the same environment.

Phase 2b: in the provider's home repository the vertical's test directory comes from the pinned
``vektra-stack`` checkout the provider's CI runs against; ``VEKTRA_STACK_ROOT`` points at it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_STACK = Path(os.environ.get("VEKTRA_STACK_ROOT", _HERE.parents[2]))
_ASSESS_TESTS = _STACK / "vektra-assess" / "tests"
if not (_ASSESS_TESTS / "conftest.py").is_file():
    raise RuntimeError(
        f"the vertical's tests are not at {_ASSESS_TESTS}: set VEKTRA_STACK_ROOT to a "
        "vektra-stack checkout (the joint suites run against it)"
    )
# the vertical's directory first, this one in front of it: a module of the same name here wins
for _p in (str(_ASSESS_TESTS), str(_HERE)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

# the vertical's conftest, under its own name: its fixtures (the hermetic environment, the
# structlog reset, the canary log capture) become this tree's, and it registers the vertical's
# strict log prefix
_spec = importlib.util.spec_from_file_location(
    "vektra_assess_tests_conftest", _ASSESS_TESTS / "conftest.py"
)
assert _spec is not None and _spec.loader is not None
_assess_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_assess_conftest)
globals().update(
    {k: v for k, v in vars(_assess_conftest).items() if not k.startswith("__")}
)

# what the provider's entry point does when the worker builds it (``build_provider``): its loggers
# get the strict redaction too, also when a test builds the client directly
from vektra_lms_moodle import LOGGER_PREFIX  # noqa: E402
from vektra_shared.log_safety import register_strict_logger_prefix  # noqa: E402

register_strict_logger_prefix(LOGGER_PREFIX)


@pytest.fixture(scope="session", autouse=True)
def _preload_recorded_manifests() -> None:
    """Read both recorded manifests once, synchronously, before any test runs: the first
    ``manifest()``/``site_for()`` call may come from an async path, which must not do file
    I/O on the event loop (CodeRabbit 4500701652)."""
    from assess_moodle_support import preload_manifests

    preload_manifests()
