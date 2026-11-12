"""``MoodleLmsProvider`` (plan 2a-2a.1, ADR-0033 D2): the conformance kit on the recorded 4.3 and
5.2 fixtures, and the normalisation of Moodle's wire vocabulary into the neutral types.

The kit is the same suite that runs on ``FakeLmsProvider`` (``test_fake_lms.py``): here it binds
to the real client over ``httpx.MockTransport``, in every transport mode it knows (recorded,
unknown shape, timeout, breaker open, raising).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from assess_moodle_support import (
    BRANCHES,
    FIXTURES,
    TOKEN,
    Replay,
    lms_site,
    make_provider,
    manifest,
    resolver,
    uid,
)

from vektra_assess.capabilities import FLAGS
from vektra_assess.config import VektraAssessSettings
from vektra_assess.moodle import domain as m
from vektra_assess.moodle.breaker import BreakerRegistry
from vektra_assess.moodle.client import MoodleWsClient
from vektra_assess.moodle.probes import derive_probe_manifest
from vektra_assess.moodle.provider import (
    FINAL_STATES,
    MOODLE_REVIEW_PROFILE,
    NO_MARK_FINAL_STATES,
    PENDING_STATES,
    QUESTION_KINDS,
    MoodleLmsProvider,
    _grace,
    _quiz,
    question_kind,
    question_state,
)
from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    GradeMethod,
    LmsProvider,
    OverdueMode,
    QuestionKind,
    QuestionState,
    Readable,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)
from vektra_testkit.lms_conformance import kit
from vektra_testkit.lms_conformance.suite import LmsConformanceSuite

ENV = {"VEKTRA_ASSESS_ENABLED": "true", "VEKTRA_ASSESS_WS_RPS": "1000"}
#: LMS-side content with no slot in any neutral type: general feedback, overall feedback,
#: marker notes, unreleased comments, the grader's essay comment (only inside the review HTML).
#: Rubric texts are left out on purpose: ``RubricDefinition`` carries them by design, and the
#: vertical's policy decides (BR-011, the canary matrix covers it).
_NO_SLOT = ("GENFB", "OVERALLFB", "GUIDEMARKERS", "UNRELEASEDFB", "ESSAYCOMMENT")


def _files_served(replay: Replay, branch: str, assignid: int) -> None:
    for f in sorted(
        (FIXTURES / branch).glob("mod_assign_get_submission_status__*.json")
    ):
        doc = json.loads(f.read_text())
        if doc["params"].get("assignid") != assignid:
            continue
        sub = doc["response"].get("lastattempt", {}).get("submission") or {}
        for plugin in sub.get("plugins", []):
            for area in plugin.get("fileareas", []):
                for file in area.get("files", []):
                    local = FIXTURES / branch / "files" / f"stu1__{file['filename']}"
                    if local.exists():
                        replay.files[urlsplit(file["fileurl"]).path] = (
                            local.read_bytes()
                        )


def _file_ref(branch: str, assignid: int, userid: int) -> tuple[SubmissionFileRef, int]:
    for f in sorted(
        (FIXTURES / branch).glob("mod_assign_get_submission_status__*.json")
    ):
        doc = json.loads(f.read_text())
        p = doc["params"]
        if p.get("assignid") != assignid or p.get("userid") != userid:
            continue
        sub = doc["response"]["lastattempt"]["submission"]
        for plugin in sub.get("plugins", []):
            for area in plugin.get("fileareas", []):
                for file in area.get("files", []):
                    ref = SubmissionFileRef(
                        filename=file["filename"],
                        filepath=file["filepath"],
                        size=int(file["filesize"]),
                        mimetype=file["mimetype"],
                        locator=file["fileurl"],
                        time_modified=int(file["timemodified"]),
                    )
                    return ref, int(sub["id"])
    raise AssertionError(f"no recorded file for assign {assignid}")


#: replay id -> the bodies its transport served; provider id -> its replay id
_SERVED: dict[int, list[str]] = {}
_REPLAY_OF: dict[int, int] = {}


def _client(replay: Replay, mode: kit.TransportMode) -> MoodleWsClient:
    settings = VektraAssessSettings(**ENV)  # type: ignore[arg-type]
    # only the breaker mode trips it: in the other modes every call must reach the transport
    breakers = BreakerRegistry(
        1 if mode is kit.TransportMode.BREAKER_OPEN else 1_000_000, 3600.0
    )
    release = manifest(replay.branch)["release"]

    def handler(request: httpx.Request) -> httpx.Response:
        match mode:
            case kit.TransportMode.TIMEOUT:
                raise httpx.ReadTimeout("timed out", request=request)
            case kit.TransportMode.RAISING:
                # the exception text carries the credential: a provider that logs it leaks it
                raise RuntimeError(f"connect failed with {TOKEN}")
            case kit.TransportMode.UNKNOWN_SHAPE:
                # a known release, so the model set is chosen; then a shape no model accepts
                return httpx.Response(
                    200, json={"release": release, "unexpected_shape": [1, 2, 3]}
                )
        response = replay.handler(request)
        # what the transport actually served: the conformance kit's canary witness
        served.append(response.content.decode("utf-8", "replace"))
        return response

    served: list[str] = []
    _SERVED[id(replay)] = served
    client = MoodleWsClient(
        settings,
        token_provider=lambda _site: TOKEN,
        breakers=breakers,
        transport=httpx.MockTransport(handler),
        resolver=resolver(),
    )
    if mode is kit.TransportMode.BREAKER_OPEN:
        breakers.for_site("site-1").record_failure()
    return client


def _target(branch: str) -> kit.ConformanceTarget:
    mf = manifest(branch)
    pm = derive_probe_manifest(mf)
    user = uid(branch, "stu1")
    quiz = mf["quizzes"]["review_all"]
    files = mf["assigns"]["files"]
    attempt = pm["required_reads"]["quiz"]["attemptid"]
    definition = pm["required_reads"]["grading"]["definitionid"]
    file_ref, submission_id = _file_ref(branch, files["id"], user)
    canaries = tuple(c for c in mf["canaries"] if any(t in c for t in _NO_SLOT))

    def factory(mode: kit.TransportMode) -> LmsProvider:
        replay = Replay(branch)
        _files_served(replay, branch, files["id"])
        client = _client(replay, mode)
        provider = MoodleLmsProvider(client, client._s, clock=lambda: 1_800_000_000)
        _REPLAY_OF[id(provider)] = id(replay)
        return provider

    def witness(provider: LmsProvider) -> str:
        """The documents THIS provider's transport served in the run (never the canaries)."""
        return "\n".join(_SERVED[_REPLAY_OF[id(provider)]])

    return kit.ConformanceTarget(
        name=f"moodle-{branch}",
        factory=factory,
        site=dataclasses.replace(lms_site(branch), release=None),
        ids=kit.ConformanceIds(
            user_id=user,
            course_id=int(mf["courseid"]),
            quiz_activity_id=int(quiz["cmid"]),
            quiz_id=int(quiz["id"]),
            assignment_activity_id=int(files["cmid"]),
            assignment_id=int(files["id"]),
            attempt_id=int(attempt),
            definition_id=int(definition),
            submission_id=submission_id,
            file_ref=file_ref,
            foreign_activity_id=999_999,
            foreign_course_id=999_998,
        ),
        canaries=canaries,
        canary_witness=witness,
        secrets=(TOKEN,),
        flags=FLAGS,
        manifest=pm,
    )


def _with_release(t: kit.ConformanceTarget, branch: str) -> kit.ConformanceTarget:
    """The target on the branch's site, its canaries narrowed to the no-slot canaries the
    target's own read paths serve (a canary in a recording the kit never reads would make the
    witness check fail as vacuous, rightly): a dry run lists them, and at least one remains."""
    t = dataclasses.replace(t, site=lms_site(branch))
    provider = t.factory(kit.TransportMode.RECORDED)
    asyncio.run(kit.read_all(provider, t))
    served = t.canary_witness(provider)
    canaries = tuple(c for c in t.canaries if c in served)
    assert canaries, f"no no-slot canary in the {branch} documents the kit reads"
    return dataclasses.replace(t, canaries=canaries)


class TestMoodle43Conformance(LmsConformanceSuite):
    target = _with_release(_target("4.3"), "4.3")


class TestMoodle52Conformance(LmsConformanceSuite):
    target = _with_release(_target("5.2"), "5.2")


def test_the_kit_targets_plant_canaries() -> None:
    """False-pass guard: each branch carries no-slot canaries for the kit to look for."""
    for branch in BRANCHES:
        assert len(_target(branch).canaries) >= 10, branch


# --- normalisation ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(FINAL_STATES - NO_MARK_FINAL_STATES))
def test_a_final_state_with_a_mark(state: str) -> None:
    assert question_state(state) is QuestionState.FINAL_MARKED


@pytest.mark.parametrize("state", sorted(NO_MARK_FINAL_STATES))
def test_a_final_state_without_a_mark(state: str) -> None:
    assert question_state(state) is QuestionState.FINAL_NO_MARK


@pytest.mark.parametrize("state", sorted(PENDING_STATES))
def test_a_pending_state(state: str) -> None:
    assert question_state(state) is QuestionState.PENDING


@pytest.mark.parametrize("state", [None, "", "gradedsomething", "GRADEDRIGHT"])
def test_an_unknown_state_is_unknown_never_final(state: str | None) -> None:
    assert question_state(state) is QuestionState.UNKNOWN


def test_the_question_kinds() -> None:
    assert QUESTION_KINDS == {
        "multichoice": QuestionKind.MULTIPLE_CHOICE,
        "truefalse": QuestionKind.TRUE_FALSE,
        "shortanswer": QuestionKind.SHORT_ANSWER,
        "numerical": QuestionKind.NUMERICAL,
        "essay": QuestionKind.ESSAY,
        "match": QuestionKind.MATCHING,
        "multianswer": QuestionKind.EMBEDDED_ANSWERS,
        "gapselect": QuestionKind.GAP_SELECT,
        "description": QuestionKind.INFORMATION,
    }
    for qtype in ("ddwtos", "ddmarker", "calculated", ""):
        assert question_kind(qtype) is QuestionKind.OTHER


@pytest.mark.parametrize(
    ("wire", "mode"),
    [
        ("autosubmit", OverdueMode.AUTOSUBMIT),
        ("graceperiod", OverdueMode.GRACE),
        ("autoabandon", OverdueMode.ABANDON),
    ],
)
def test_overdue_handling(wire: str, mode: OverdueMode) -> None:
    assert _grace(m.GracePolicy(60, wire)).overdue is mode


def _quiz_inputs(**over: Any) -> m.QuizCloseInputs:
    base = m.QuizCloseInputs(
        quizid=1,
        courseid=2,
        name="q",
        timeopen=0,
        timeclose=100,
        timelimit_s=0,
        grademethod="highest",
        attempts_allowed=0,
        grace=Readable(m.GracePolicy(60, "graceperiod")),
        review_after_close=Unreadable(UnreadableReason.PARTIAL),
        overrides=Readable(()),
    )
    return dataclasses.replace(base, **over)


def test_an_unknown_overdue_mode_makes_the_grace_unreadable() -> None:
    q = _quiz(_quiz_inputs(grace=Readable(m.GracePolicy(60, "somethingnew"))))
    assert q.grace == Unreadable(UnreadableReason.UNKNOWN_SHAPE, "overduehandling")
    assert q.time_close == 100  # the rest of the quiz stays readable


@pytest.mark.parametrize("method", ["highest", "average", "first", "last"])
def test_grade_methods(method: str) -> None:
    assert _quiz(_quiz_inputs(grademethod=method)).grade_method is GradeMethod(method)


async def test_an_unknown_grade_method_is_unknown_shape() -> None:
    replay = Replay("4.3")

    def mutate(fn: str, body: Any) -> Any:
        if fn == "mod_quiz_get_quizzes_by_courses":
            for q in body["quizzes"]:
                q["grademethod"] = 9
        return body

    replay.mutate = mutate
    mf = manifest("4.3")
    q = mf["quizzes"]["review_all"]
    got = await make_provider(replay).quiz_settings(
        lms_site("4.3"), int(mf["courseid"]), int(q["id"])
    )
    assert isinstance(got, Unreadable)
    assert got.reason is UnreadableReason.UNKNOWN_SHAPE


@pytest.mark.parametrize("branch", BRANCHES)
async def test_the_re_resolution_refusal_has_the_neutral_code(branch: str) -> None:
    got = await make_provider(Replay(branch)).resolve_activity(
        lms_site(branch),
        user_id=uid(branch, "stu1"),
        activity_id=999_999,
        kind="quiz",
        course_hint=None,
    )
    assert isinstance(got, Unreadable) and got.detail_code == NOT_IN_USER_COURSES


@pytest.mark.parametrize("branch", BRANCHES)
async def test_reviews_carry_the_moodle_profile(branch: str) -> None:
    mf = manifest(branch)
    attempt = derive_probe_manifest(mf)["required_reads"]["quiz"]["attemptid"]
    got = await make_provider(Replay(branch)).attempt_review(
        lms_site(branch), int(attempt)
    )
    assert isinstance(got, Readable) and got.value.items
    htmls = [i.html for i in got.value.items if i.html is not None]
    assert htmls and all(h.profile is MOODLE_REVIEW_PROFILE for h in htmls)
    assert MOODLE_REVIEW_PROFILE.keep == {
        "question_text": "qtext",
        "response": "ablock",
    }
    assert {"rightanswer", "specificfeedback", "generalfeedback"} <= (
        MOODLE_REVIEW_PROFILE.drop_classes
    )


def test_startup_refuses_plain_http_outside_development() -> None:
    allow = VektraAssessSettings(  # type: ignore[arg-type]
        VEKTRA_ASSESS_ENABLED="true",
        VEKTRA_ENV="development",
        VEKTRA_ASSESS_MOODLE_ALLOW_HTTP="true",
    )
    p = make_provider(Replay("4.3"))
    p_http = MoodleLmsProvider(p.client, allow)
    assert p_http.startup_problems(True) == ()
    (problem,) = p_http.startup_problems(False)
    assert problem.setting == "VEKTRA_ASSESS_MOODLE_ALLOW_HTTP"
    assert "VEKTRA_ENV=development" in problem.detail
    assert p.startup_problems(False) == ()


def test_the_provider_implements_the_protocol_and_names_itself() -> None:
    p = make_provider(Replay("4.3"))
    assert isinstance(p, LmsProvider)
    assert p.name == "moodle"
    assert (
        "ws_tls_ok" in p.setup_items and "lms_provider_registered" not in p.setup_items
    )
