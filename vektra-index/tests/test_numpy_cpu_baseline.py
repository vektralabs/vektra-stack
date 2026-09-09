"""Guard: the numpy wheel we install must run on a pre-x86-64-v2 CPU.

The production VM's CPU predates x86-64-v2 (SSE4.2 present, POPCNT absent,
no AVX). Every numpy >= 2.0 x86_64 wheel on PyPI is compiled with an X86_V2
baseline, and numpy checks that baseline at import time: on that host the
interpreter aborts before any Vektra code runs, with

    RuntimeError: NumPy was built with baseline optimizations: (X86_V2)
    but your machine doesn't support: (X86_V2).

`[tool.uv] constraint-dependencies = ["numpy<2"]` in the root pyproject keeps
the resolution on numpy 1.26, whose x86_64 wheels carry an SSE3 baseline. That
constraint is one line in a file nobody reads twice, and numpy arrives here
transitively through half the ML stack, so a future `uv lock` could quietly
walk it forward. This test is what notices.

`__cpu_baseline__` is a property of the *wheel*, not of the machine running
the test, so this assertion is meaningful on any x86_64 CI runner even though
every GitHub runner supports far more than the production VM does. It cannot
be checked on arm64 (Apple silicon dev machines), where the baseline is the
NEON set, so the baseline assertion is skipped there and only the version
bound is enforced.
"""

from __future__ import annotations

import platform

import numpy
import pytest

# x86-64-v1 baseline plus SSE3, which is what numpy 1.26 x86_64 wheels are
# built with and what the production CPU provides. Anything beyond this set
# (SSSE3, SSE41, POPCNT, SSE42, AVX*, F16C, FMA3) means the wheel demands a
# newer CPU than the deployment target has.
ALLOWED_BASELINE_FEATURES = {"SSE", "SSE2", "SSE3"}

_X86 = platform.machine().lower() in {"x86_64", "amd64"}


def _cpu_baseline() -> list[str]:
    """Read numpy's compiled-in CPU baseline, across the 1.x/2.x rename."""
    try:  # numpy >= 2.0
        from numpy._core._multiarray_umath import (  # type: ignore[import-not-found]
            __cpu_baseline__,
        )
    except ImportError:  # numpy 1.x
        from numpy.core._multiarray_umath import (  # type: ignore[import-not-found,no-redef]
            __cpu_baseline__,
        )
    return list(__cpu_baseline__)


def test_numpy_major_version_is_pinned_below_2() -> None:
    """The installed numpy must be the 1.x line the constraint asks for."""
    major = int(numpy.__version__.split(".")[0])
    assert major < 2, (
        f"numpy {numpy.__version__} is installed, but the deployment target "
        "cannot import numpy >= 2 (X86_V2 baseline). Either the root "
        '`[tool.uv] constraint-dependencies = ["numpy<2"]` was dropped, or '
        "this environment is out of sync with uv.lock (`uv sync --dev`)."
    )


@pytest.mark.skipif(not _X86, reason="CPU baseline check only applies to x86_64 wheels")
def test_numpy_cpu_baseline_runs_on_pre_v2_hardware() -> None:
    """The wheel must not require CPU features the production VM lacks."""
    baseline = _cpu_baseline()
    too_new = sorted(set(baseline) - ALLOWED_BASELINE_FEATURES)
    assert not too_new, (
        f"numpy {numpy.__version__} was built with baseline optimizations "
        f"{baseline}, which require {too_new} — CPU features the production "
        "VM does not have. Importing this wheel there aborts the interpreter "
        "before startup. Keep the resolution on numpy 1.26 (see the root "
        "pyproject constraint) or publish a separate image for that host."
    )
