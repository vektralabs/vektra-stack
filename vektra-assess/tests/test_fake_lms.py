"""``FakeLmsProvider`` (plan 2a-1.8): the conformance kit runs green on it, and the kit says no
to a provider broken on purpose (the negative controls that prove the kit is not vacuous)."""

from __future__ import annotations

import dataclasses

import pytest
import structlog
from support.fake_lms import (
    ALL,
    ASSIGN_ACTIVITY,
    ASSIGNMENT,
    ATTEMPT,
    COURSE,
    DEFINITION,
    FOREIGN_ACTIVITY,
    LOCATOR,
    OTHER_COURSE,
    QUIZ,
    QUIZ_ACTIVITY,
    SITE,
    SUBMISSION,
    USER,
    FakeLmsProvider,
    default_world,
)

from vektra_assess.capabilities import FLAGS
from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    PROVIDER_ERROR,
    ActivityRef,
    LmsProvider,
    Readable,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)
from vektra_shared.registry import ProviderRegistry
from vektra_testkit.lms_conformance import kit
from vektra_testkit.lms_conformance.suite import LmsConformanceSuite

SECRET = "fake-transport-credential-9f8e7d"
FILE_REF = SubmissionFileRef("essay.txt", "/", 5, "text/plain", LOCATOR, 1_700_000_000)


def _fake_for(
    mode: kit.TransportMode, cls: type[FakeLmsProvider] = FakeLmsProvider
) -> FakeLmsProvider:
    fake = cls(secret=SECRET)
    match mode:
        case kit.TransportMode.RECORDED:
            pass
        case kit.TransportMode.RAISING:
            # the exception text carries the credential: a provider that logged it would leak
            fake.transport_error(ALL, RuntimeError(f"connect failed with {SECRET}"))
        case _:
            reason = mode.expected_reason
            assert reason is not None
            fake.fail(ALL, Unreadable(reason, "fake"))
    return fake


def _target(
    cls: type[FakeLmsProvider] = FakeLmsProvider, name: str = "fake"
) -> kit.ConformanceTarget:
    world = default_world()
    canaries = tuple(world.hidden.values())

    def witness() -> str:
        review = world.reviews[ATTEMPT]
        return repr(world.hidden) + "".join(
            i.html.html for i in review.items if i.html is not None
        )

    return kit.ConformanceTarget(
        name=name,
        factory=lambda mode: _fake_for(mode, cls),
        site=SITE,
        ids=kit.ConformanceIds(
            user_id=USER,
            course_id=COURSE,
            quiz_activity_id=QUIZ_ACTIVITY,
            quiz_id=QUIZ,
            assignment_activity_id=ASSIGN_ACTIVITY,
            assignment_id=ASSIGNMENT,
            attempt_id=ATTEMPT,
            definition_id=DEFINITION,
            submission_id=SUBMISSION,
            file_ref=FILE_REF,
            foreign_activity_id=FOREIGN_ACTIVITY,
            foreign_course_id=OTHER_COURSE,
        ),
        canaries=canaries,
        canary_witness=witness,
        secrets=(SECRET,),
        flags=FLAGS,
    )


class TestFakeLmsConformance(LmsConformanceSuite):
    target = _target()


# --- negative controls: each broken provider turns exactly its check red -----------------------


class _Escapes(FakeLmsProvider):
    """Lets the transport exception out instead of answering Unreadable."""

    def transport_error(self, member: str, exc: BaseException) -> FakeLmsProvider:
        return self.escape(member, exc)


class _TrustsTheHint(FakeLmsProvider):
    """Resolves into the hinted course: the REQ-095 hole."""

    async def resolve_activity(self, site, *, user_id, activity_id, kind, course_hint):  # type: ignore[no-untyped-def,override]
        if course_hint is not None:
            return Readable(ActivityRef(course_hint, activity_id))
        return await super().resolve_activity(
            site, user_id=user_id, activity_id=activity_id, kind=kind, course_hint=None
        )


class _LeaksHiddenFeedback(FakeLmsProvider):
    async def submission(self, site, assignment_id, user_id):  # type: ignore[no-untyped-def,override]
        res = await super().submission(site, assignment_id, user_id)
        assert isinstance(res, Readable) and res.value.feedback is not None
        fb = dataclasses.replace(
            res.value.feedback, comments=(self.world.hidden["hidden_feedback"],)
        )
        return Readable(dataclasses.replace(res.value, feedback=fb))


class _LogsItsCredential(FakeLmsProvider):
    async def site_info(self, site):  # type: ignore[no-untyped-def,override]
        # a message is an event name, kept verbatim by the allowlist: the leak survives it
        structlog.get_logger("vektra_lms_fake").info(f"auth {self.secret}")
        return await super().site_info(site)


class _ForgetsAnItem(FakeLmsProvider):
    async def service_account_check(self, site, info, manifest):  # type: ignore[no-untyped-def,override]
        return (await super().service_account_check(site, info, manifest))[1:]


class _AnswersNothing(FakeLmsProvider):
    """Unreadable everywhere: would pass every fault check, so the recorded check must catch it."""

    def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **kw)
        self.fail(ALL, Unreadable(UnreadableReason.PARTIAL, "x"))


async def test_a_provider_that_raises_is_red() -> None:
    with pytest.raises(AssertionError, match="let RuntimeError escape"):
        await kit.check_fault_mode(_target(_Escapes), kit.TransportMode.RAISING)


async def test_a_provider_that_returns_the_hinted_course_is_red() -> None:
    with pytest.raises(AssertionError):
        await kit.check_re_resolution(_target(_TrustsTheHint))


async def test_a_provider_that_leaks_a_canary_is_red() -> None:
    with pytest.raises(AssertionError, match="submission returned forbidden content"):
        await kit.check_no_canary(_target(_LeaksHiddenFeedback))


async def test_a_provider_that_logs_its_credential_is_red() -> None:
    with pytest.raises(AssertionError, match="credential"):
        await kit.check_no_secret_in_logs(_target(_LogsItsCredential))


async def test_a_provider_that_drops_a_declared_item_is_red() -> None:
    with pytest.raises(AssertionError, match="declared but not returned"):
        await kit.check_setup_items(_target(_ForgetsAnItem))


async def test_a_provider_that_answers_nothing_is_red() -> None:
    with pytest.raises(AssertionError, match="Unreadable on its own recording"):
        await kit.check_recorded_reads(_target(_AnswersNothing))


# --- the fake's own contract ------------------------------------------------------------------


def test_it_is_a_provider_and_a_refused_test_double() -> None:
    fake = FakeLmsProvider()
    assert isinstance(fake, LmsProvider)
    with pytest.raises(ValueError, match="test double"):
        ProviderRegistry().register("lms", "fake", fake)
    ProviderRegistry(allow_test_doubles=True).register("lms", "fake", fake)


async def test_the_recorder_sees_a_call_even_when_it_is_scripted_to_fail() -> None:
    fake = FakeLmsProvider().fail("enrolment", Unreadable(UnreadableReason.TIMEOUT))
    res = await fake.enrolment(SITE, COURSE, USER)
    assert isinstance(res, Unreadable) and res.reason is UnreadableReason.TIMEOUT
    assert fake.count("enrolment") == 1
    fake.assert_never_read("attempt_review", "submission_file")
    with pytest.raises(AssertionError, match="enrolment"):
        fake.assert_never_read("enrolment")


async def test_a_transport_error_is_unreadable_and_an_escape_is_raised() -> None:
    fake = FakeLmsProvider().transport_error("attempts", OSError("x"))
    res = await fake.attempts(SITE, QUIZ, USER)
    assert res == Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)
    fake.clear()
    fake.escape("attempts", OSError("x"))
    with pytest.raises(OSError):
        await fake.attempts(SITE, QUIZ, USER)


async def test_resolution_ignores_the_hint() -> None:
    fake = FakeLmsProvider()
    own = await fake.resolve_activity(
        SITE,
        user_id=USER,
        activity_id=QUIZ_ACTIVITY,
        kind="quiz",
        course_hint=OTHER_COURSE,
    )
    assert isinstance(own, Readable) and own.value.course_id == COURSE
    foreign = await fake.resolve_activity(
        SITE, user_id=USER, activity_id=FOREIGN_ACTIVITY, kind="quiz", course_hint=None
    )
    assert (
        isinstance(foreign, Unreadable) and foreign.detail_code == NOT_IN_USER_COURSES
    )


async def test_a_file_is_served_only_to_its_own_submission() -> None:
    fake = FakeLmsProvider()
    ok = await fake.submission_file(SITE, ASSIGNMENT, FILE_REF, USER, SUBMISSION)
    assert ok == Readable(b"essay")
    other = await fake.submission_file(SITE, ASSIGNMENT, FILE_REF, USER, SUBMISSION + 1)
    assert isinstance(other, Unreadable)


def test_the_vertical_re_export_is_the_same_object() -> None:
    """``vektra_assess.readable`` stays a re-export until 2a-2b deletes it (plan 2a-1.1): one
    class, not two, or an isinstance check across the boundary would silently fail."""
    from vektra_assess import readable
    from vektra_assess.moodle import result

    for mod in (readable, result):
        assert mod.Readable is Readable and mod.Unreadable is Unreadable
        assert mod.UnreadableReason is UnreadableReason
