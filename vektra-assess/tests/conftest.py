"""Test environment isolation for vektra-assess (DEBT-029).

The fixture is autouse and shared: importing it here registers it for every test in
this package. See `vektra_shared.testing` for why a scrub alone is not enough.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
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


@pytest.fixture(scope="session", autouse=True)
def _preload_recorded_manifests() -> None:
    """Read both recorded manifests once, synchronously, before any test runs: the first
    ``manifest()``/``site_for()`` call may come from an async path, which must not do file
    I/O on the event loop (CodeRabbit 4500701652)."""
    from assess_moodle_support import preload_manifests

    preload_manifests()


@pytest.fixture(autouse=True)
def _fresh_structlog():
    """A structlog logger caches ``sys.stdout`` when it is first used. A test that ran under
    output capture leaves a closed file behind, and the next test that logs through the same
    module-level logger then dies with "I/O operation on closed file". Start every test from
    the default configuration so each one binds the stream that is current."""
    structlog.reset_defaults()
    yield


class CanaryLogCapture:
    """Every log line the code under test renders, through the production log-safety chain.

    structlog and the ``vektra_assess`` stdlib logger are configured exactly as the worker
    configures them (``configure_assess_worker_logging``) and write to one buffer, at DEBUG.
    ``extra_text`` lets a test add other rendered output (a sidecar's stderr, captured process
    output). ``assert_clean`` first asserts the sink is NOT empty, so an unwired capture cannot
    pass vacuously, then that no canary appears anywhere.
    """

    def __init__(self) -> None:
        import io

        self.buffer = io.StringIO()
        self.extra_text: list[str] = []

    def text(self) -> str:
        return self.buffer.getvalue() + "".join(self.extra_text)

    def lines(self) -> list[str]:
        return [ln for ln in self.text().splitlines() if ln.strip()]

    def assert_clean(
        self, canaries: Iterable[str], *, require_lines: bool = True
    ) -> None:
        from vektra_testkit.recorders import assert_no_canaries

        if require_lines:
            assert self.lines(), (
                "log sink is empty: the capture is not wired to the code"
            )
        assert_no_canaries(self.text(), canaries, sink="logs")


@pytest.fixture
def canary_log_capture() -> Iterator[CanaryLogCapture]:
    import logging

    from vektra_shared.log_safety import configure_assess_worker_logging

    cap = CanaryLogCapture()
    configure_assess_worker_logging(cap.buffer, level=logging.DEBUG)
    try:
        yield cap
    finally:
        structlog.reset_defaults()
        for name in ("", "vektra_assess"):
            lg = logging.getLogger(name)
            for h in [h for h in lg.handlers if getattr(h, "_vektra_assess", False)]:
                lg.removeHandler(h)
        logging.getLogger("vektra_assess").setLevel(logging.NOTSET)
        logging.getLogger().setLevel(logging.WARNING)
