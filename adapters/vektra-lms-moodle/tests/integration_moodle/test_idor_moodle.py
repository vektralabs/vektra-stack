"""M2.6 / NFR-014: the vertical's IDOR negative matrix (``test_idor._matrix``) with student B's
snapshot captured through the Moodle provider, on the recorded 4.3 and 5.2 responses. The live
suite (``test_idor_live.py``) runs the same function against a seeded dev site. Integration
(testcontainers Postgres).
"""

from __future__ import annotations

import pytest
from assess_moodle_support import (
    BRANCHES,
    Replay,
    make_provider,
    manifest,
    site_for,
    uid,
)
from integration.conftest import pg  # noqa: F401  (the real-Postgres fixture)
from test_idor import _matrix

pytestmark = [pytest.mark.integration]


async def idor_matrix_on_moodle(pg, branch: str) -> None:  # noqa: F811
    """The matrix with B's snapshot captured through the Moodle provider (the live suite
    points ``Replay`` at a seeded dev site with ``use_live``)."""
    m = manifest(branch)
    await _matrix(
        pg,
        make_provider(Replay(branch)),
        release=m["release"],
        base_url=site_for(branch).base_url,
        a_cmid=int(m["assigns"]["rubric_vis"]["cmid"]),
        captured_user=uid(branch, "stu1"),
    )


@pytest.mark.parametrize("branch", BRANCHES)
async def test_idor_matrix_on_the_recordings(pg, branch: str) -> None:  # noqa: F811
    await idor_matrix_on_moodle(pg, branch)
