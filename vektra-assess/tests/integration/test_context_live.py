"""Live subset of the capture tests (plan assess-context G): the real seeded 4.3 / 5.2 envs.

``pytest vektra-assess -m moodle`` with the same variables as ``test_moodle_live.py``:

    VEKTRA_ASSESS_MOODLE43_URL / VEKTRA_ASSESS_MOODLE52_URL
    VEKTRA_ASSESS_MOODLE_MANIFEST_DIR (manifest-m43.json, manifest-m52.json)
    VEKTRA_ASSESS_MOODLE_TOKEN_DIR (provision-m43.json, provision-m52.json)

One capture per shape and type (the "types" quiz holds all 9 question types), with the same sink
assertions as the hermetic matrix: no seeded canary in the snapshot plaintext, the bundle, Part A
or the request bytes; question text present for the extraction-enabled types. The parser is the
sidecar's own dispatch in-process. Without the variables every test SKIPs with ``BLOCKED: ...``.
"""

from __future__ import annotations

from typing import Any

import pytest
from assess_moodle_support import (
    BRANCHES,
    Replay,
    manifest,
    site_readers,
    uid,
    use_live,
)
from canary.runner import real_turn
from test_context_capture import OFF, InProcessParser

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.capture.bound import SiteReaders
from vektra_assess.context.capture.capture import (
    AssignTarget,
    Built,
    Capabilities,
    CaptureNotEligible,
    QuizTarget,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.capture.selection import NotEligibleReason
from vektra_assess.context.part_a import SessionFacts, render_part_a
from vektra_assess.context.snapshot import canonical_bytes
from vektra_testkit.recorders import (
    RecordingVectorStoreProvider,
    find_canaries,
)

from .test_moodle_live import _load

pytestmark = [
    pytest.mark.integration,
    pytest.mark.moodle,
    pytest.mark.parametrize("branch", BRANCHES),
]


@pytest.fixture(autouse=True)
def live(branch: str):  # type: ignore[no-untyped-def]
    env = _load(branch)
    use_live(branch, env)
    yield env
    use_live(branch, None)


def _readers(branch: str) -> tuple[Replay, SiteReaders, dict[str, Any]]:
    replay = Replay(branch)
    return (
        replay,
        site_readers(replay),
        manifest(branch),
    )


async def _sinks(built: Built) -> dict[str, bytes]:
    bundle = assemble_context(built.snapshot, built.frozen_policy)
    vs = RecordingVectorStoreProvider(forbid_calls=True)
    turn = await real_turn(
        bundle, vs
    )  # the real DiscussionTurnRunner (plan assess-llm-runner)
    assert vs.calls == []
    out = {
        "snapshot": canonical_bytes(built.snapshot),
        "bundle": bundle.model_dump_json().encode(),
        "part_a": render_part_a(bundle, SessionFacts(1, 30)).text.encode(),
        "request_bytes": turn["request_bytes"],
        "query_trace": turn["query_trace"],
        "released": turn["released"],
    }
    assert all(out.values()), "every sink is non-empty"
    return out


def _clean(sinks: dict[str, bytes], canaries: list[str]) -> None:
    for name, blob in sinks.items():
        hits = {c: n for c, n in find_canaries(blob, canaries).items() if n}
        assert hits == {}, (name, sorted(hits))


async def test_live_quiz_all_types(branch: str) -> None:
    _r, readers, m = _readers(branch)
    q = m["quizzes"]["types"]
    built = await capture_quiz(
        QuizTarget(m["courseid"], q["id"], q["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), lms_release=m["release"],
    )  # fmt: skip
    assert isinstance(built, Built), built
    sinks = await _sinks(built)
    _clean(sinks, list(m["canaries"]))
    for qt in ("tf", "mc", "sa", "num"):
        assert f"VISIBLE-QTEXT-{qt}".encode() in sinks["bundle"]


@pytest.mark.parametrize("name", ["rubric_vis", "rubric_hidden", "simple", "scale"])
async def test_live_assignment_shapes(branch: str, name: str) -> None:
    _r, readers, m = _readers(branch)
    a = m["assigns"][name]
    built = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), lms_release=m["release"],
    )  # fmt: skip
    assert isinstance(built, Built), (name, built)
    _clean(await _sinks(built), list(m["canaries"]))


async def test_live_unreleased_grade_is_not_eligible(branch: str) -> None:
    _r, readers, m = _readers(branch)
    a = m["assigns"]["hiddengrade"]
    out = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, "stu1")),
        readers, InProcessParser(), OFF, Capabilities(), lms_release=m["release"],
    )  # fmt: skip
    assert out == CaptureNotEligible(NotEligibleReason.NOT_RELEASED)
