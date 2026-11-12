"""M2.6 on the LIVE seeded dev sites (``-m moodle``): the IDOR matrix with B's snapshot captured
from the real 4.3 / 5.2 Moodle instead of the recordings.

SKIPPED with ``BLOCKED: ...`` unless the dev env variables of ``test_moodle_live`` are set (the
integration job lists this module among the live ones that may skip; the
``assess-moodle-contract`` workflow runs it against the two dev sites).
"""

from __future__ import annotations

import pytest
from assess_moodle_support import BRANCHES, use_live

from .test_idor_moodle import idor_matrix_on_moodle as run_matrix
from .test_moodle_live import _load

pytestmark = [
    pytest.mark.integration,
    pytest.mark.moodle,
    pytest.mark.parametrize("branch", BRANCHES),
]


async def test_idor_matrix_live(pg, branch: str) -> None:  # type: ignore[no-untyped-def]
    env = _load(branch)  # skips (BLOCKED) when the dev env is not configured
    use_live(branch, env)
    try:
        await run_matrix(pg, branch)
    finally:
        use_live(branch, None)
