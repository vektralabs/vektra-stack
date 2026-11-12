"""Live contract tests: the SAME reader tests, against the seeded Moodle 4.3 / 5.2 dev envs.

``pytest vektra-assess -m moodle`` with

    VEKTRA_ASSESS_MOODLE43_URL=http://localhost:8080
    VEKTRA_ASSESS_MOODLE52_URL=http://localhost:8082
    VEKTRA_ASSESS_MOODLE_MANIFEST_DIR=<dir with manifest-m43.json, manifest-m52.json>
    VEKTRA_ASSESS_MOODLE_TOKEN_DIR=<dir with provision-m43.json, provision-m52.json>

The tests read the service-account token from ``provision-<tag>.json`` (key
``service_account.token``); nothing is hard-coded. Without the variables every test SKIPs with
the reason ``BLOCKED: ...``; the CI contract workflow fails if the collected count is 0.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_moodle_readers as hermetic
from assess_moodle_support import (
    BRANCHES,
    LiveEnv,
    Replay,
    make_client,
    site_for,
    uid,
    use_live,
)
from structlog.testing import capture_logs

from vektra_assess.moodle.readers import (
    fetch_submission_file,
    read_attempt_review,
    read_enrolment,
    read_graders,
    read_grading_view,
    read_site_info,
    read_submission_status,
    read_user_flags,
)
from vektra_assess.moodle.result import Readable, Unreadable, UnreadableReason

# read at import: the autouse hermetic env scrub clears VEKTRA_* before a test body runs
_ENV = {
    "4.3": ("VEKTRA_ASSESS_MOODLE43_URL", "m43"),
    "5.2": ("VEKTRA_ASSESS_MOODLE52_URL", "m52"),
}
_URL = {b: os.environ.get(var) for b, (var, _tag) in _ENV.items()}
_MANIFESTS = os.environ.get("VEKTRA_ASSESS_MOODLE_MANIFEST_DIR")
_TOKENS = os.environ.get("VEKTRA_ASSESS_MOODLE_TOKEN_DIR")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.moodle,
    pytest.mark.parametrize("branch", BRANCHES),
]


def _load(branch: str) -> LiveEnv:
    var, tag = _ENV[branch]
    if not _URL[branch]:
        pytest.skip(f"BLOCKED: {var} unset")
    if not _MANIFESTS or not _TOKENS:
        pytest.skip("BLOCKED: VEKTRA_ASSESS_MOODLE_MANIFEST_DIR / ..._TOKEN_DIR unset")
    manifest = json.loads((Path(_MANIFESTS) / f"manifest-{tag}.json").read_text())
    prov = json.loads((Path(_TOKENS) / f"provision-{tag}.json").read_text())
    return LiveEnv(str(_URL[branch]), manifest, prov["service_account"]["token"])


@pytest.fixture(autouse=True)
def live(branch: str) -> Iterator[LiveEnv]:
    env = _load(branch)
    use_live(branch, env)
    yield env
    use_live(branch, None)


#: the hermetic reader tests that are valid against a real site (no mutation, no recordings)
LIVE_TESTS = [
    "test_site_info",
    "test_quiz_close_inputs_match_the_manifest",
    "test_quiz_overrides",
    "test_user_attempts",
    "test_attempt_review",
    "test_needs_grading_is_per_slot",
    "test_review_html_is_separate_and_droppable",
    "test_user_review_options",
    "test_assign_settings",
    "test_assign_close_inputs_effective_dates",
    "test_user_flags_are_unreadable_for_the_minimal_role",
    "test_submission_status",
    "test_submission_status_grading_status_and_feedback",
    "test_grade_release_from_the_grade_report",
    "test_assign_release_state",
    "test_grading_view",
    "test_rubric_fillings",
    "test_rubric_display_options_are_unreadable_by_design",
    "test_scale_definition",
    "test_enrolment",
    "test_graders",
    "test_expected_unreadable_readers",
]


@pytest.mark.parametrize("name", LIVE_TESTS)
async def test_reader_against_the_live_site(name: str, branch: str) -> None:
    # the hermetic tests are parametrised by ``branch`` and call ``env(branch)``, which now
    # resolves to the live site, its manifest and its token
    test: Callable[[str], Any] = getattr(hermetic, name)
    result = test(branch)
    if hasattr(result, "__await__"):
        await result


async def test_live_file_fetch_matches_the_manifest(branch: str, live: LiveEnv) -> None:
    m = live.manifest
    a = m["assigns"]["files"]
    replay = Replay(branch)
    client = make_client(replay)
    site = site_for(branch)
    owner = uid(branch, "stu1")
    status = await read_submission_status(client, site, a["id"], owner)
    assert isinstance(status, Readable)
    want = {f["filename"]: f for f in a["submissions"]["stu1"]["files"]}
    assert {f.filename for f in status.value.files} == set(want)
    for ref in status.value.files:
        got = await fetch_submission_file(
            client, site, a["id"], ref, owner, status.value.submission_id or 0
        )
        assert isinstance(got, Readable), ref.filename
        assert len(got.value) == ref.size
        canary = want[ref.filename].get("content_canary")
        if canary and ref.mimetype.startswith("text"):
            assert canary.encode() in got.value
    # another student's id with the owner's file reference: refused before any download
    other = await fetch_submission_file(
        client,
        site,
        a["id"],
        status.value.files[0],
        uid(branch, "stu2"),
        status.value.submission_id or 0,
    )
    assert isinstance(other, Unreadable)
    # a bad token is a typed Unreadable, not an exception
    bad = make_client(replay)
    bad._token_for = lambda _s: "0" * 32  # type: ignore[method-assign]
    res = await read_site_info(bad, site)
    assert (
        isinstance(res, Unreadable) and res.reason is UnreadableReason.ACCESS_EXCEPTION
    )


async def test_live_run_logs_no_token_canary_or_body(
    branch: str, live: LiveEnv
) -> None:
    m = live.manifest
    replay = Replay(branch)
    client = make_client(replay)
    site = site_for(branch)
    a = m["assigns"]["files"]
    with capture_logs() as logs:
        await read_site_info(client, site)
        status = await read_submission_status(
            client, site, a["id"], uid(branch, "stu1")
        )
        await read_attempt_review(
            client, site, m["quizzes"]["review_none"]["attempts"][0]["id"]
        )
        await read_grading_view(client, site, m["assigns"]["rubric_vis"]["cmid"])
        await read_graders(client, site, m["courseid"])
        await read_user_flags(client, site, m["assigns"]["cutoff"]["id"])
        await read_enrolment(client, site, m["courseid"], uid(branch, "stu1"))
        assert isinstance(status, Readable)
        await fetch_submission_file(
            client,
            site,
            a["id"],
            status.value.files[0],
            uid(branch, "stu1"),
            status.value.submission_id or 0,
        )
    text = json.dumps(logs, default=str)
    assert logs
    assert live.token not in text
    assert live.url not in text and "pluginfile" not in text
    for canary in m["canaries"]:
        assert canary not in text
