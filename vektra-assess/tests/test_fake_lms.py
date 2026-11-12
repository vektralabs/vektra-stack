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
    CANARIES,
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


def _witness(provider: LmsProvider) -> str:
    """What this fake's read paths consumed in the run: its wire documents, never the list of
    canaries (review of #203: a witness made of the canaries passes by construction)."""
    assert isinstance(provider, FakeLmsProvider)
    return provider.consumed_text()


def _target(
    cls: type[FakeLmsProvider] = FakeLmsProvider, name: str = "fake"
) -> kit.ConformanceTarget:
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
        canaries=CANARIES,
        canary_witness=_witness,
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
        wire = self.world.wire["submission"]["feedback_hidden_from_student"]
        fb = dataclasses.replace(res.value.feedback, comments=(wire,))
        return Readable(dataclasses.replace(res.value, feedback=fb))


class _LeaksTheUnreleasedGrade(FakeLmsProvider):
    """Copies the wire answer's grade-before-release into the neutral value: a number."""

    async def grade_item(self, site, course_id, user_id, activity_id):  # type: ignore[no-untyped-def,override]
        res = await super().grade_item(site, course_id, user_id, activity_id)
        assert isinstance(res, Readable)
        grade = self.world.wire["grade_item"]["grade_before_release"]
        return Readable(dataclasses.replace(res.value, grade=grade))


class _ProfileKeepsTheAnswer(FakeLmsProvider):
    """Ships a profile that forgets to drop the right-answer class."""

    async def attempt_review(self, site, attempt_id):  # type: ignore[no-untyped-def,override]
        res = await super().attempt_review(site, attempt_id)
        assert isinstance(res, Readable)
        items = tuple(
            dataclasses.replace(
                i,
                html=dataclasses.replace(
                    i.html,
                    profile=dataclasses.replace(
                        i.html.profile, drop_classes=frozenset()
                    ),
                ),
            )
            if i.html is not None
            else i
            for i in res.value.items
        )
        return Readable(dataclasses.replace(res.value, items=items))


class _ReadsNothingWithCanaries(FakeLmsProvider):
    """Its wire answers hold no canary: a green canary check on it would be vacuous."""

    def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **kw)
        self.world.wire.clear()
        for r in self.world.reviews.values():
            self.world.reviews[r.attempt_id] = r.without_html()


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


async def test_a_provider_that_leaks_an_unreleased_grade_is_red() -> None:
    """Review of #203: the canary is a number, and the walk now sees numbers."""
    with pytest.raises(AssertionError, match="grade_item returned forbidden content"):
        await kit.check_no_canary(_target(_LeaksTheUnreleasedGrade))


async def test_a_profile_that_keeps_the_right_answer_is_red() -> None:
    """Review of #203: the review HTML is walked, as its own profile leaves it."""
    with pytest.raises(
        AssertionError, match="attempt_review returned forbidden content"
    ):
        await kit.check_no_canary(_target(_ProfileKeepsTheAnswer))


async def test_canaries_no_read_path_consumed_make_the_check_red() -> None:
    """Review of #203: the anti-vacuous guard holds against what was really read."""
    with pytest.raises(AssertionError, match="vacuous"):
        await kit.check_no_canary(_target(_ReadsNothingWithCanaries))


async def test_a_provider_that_logs_its_credential_is_red() -> None:
    with pytest.raises(AssertionError, match="credential"):
        await kit.check_no_secret_in_logs(_target(_LogsItsCredential))


async def test_a_provider_that_drops_a_declared_item_is_red() -> None:
    with pytest.raises(AssertionError, match="declared but not returned"):
        await kit.check_setup_items(_target(_ForgetsAnItem))


async def test_a_provider_that_answers_nothing_is_red() -> None:
    with pytest.raises(AssertionError, match="Unreadable on its own recording"):
        await kit.check_recorded_reads(_target(_AnswersNothing))


# --- CodeRabbit round on #203 -------------------------------------------------------------------


class _RefusesOnlyWithAHint(FakeLmsProvider):
    """Refuses a foreign activity when a hint is passed, resolves it when none is (4543186557)."""

    async def resolve_activity(self, site, *, user_id, activity_id, kind, course_hint):  # type: ignore[no-untyped-def,override]
        if course_hint is None:
            ref = self.world.activities.get((kind, activity_id))
            if ref is not None:
                return Readable(ref)
        return await super().resolve_activity(
            site,
            user_id=user_id,
            activity_id=activity_id,
            kind=kind,
            course_hint=course_hint,
        )


async def test_a_foreign_activity_resolved_without_a_hint_is_red() -> None:
    with pytest.raises(AssertionError, match="without a course hint"):
        await kit.check_re_resolution(_target(_RefusesOnlyWithAHint))


class _UnreadableForTheWrongReason(FakeLmsProvider):
    def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **kw)
        self.fail("grade_item", Unreadable(UnreadableReason.TIMEOUT))


async def test_a_declared_unreadable_for_another_reason_is_red() -> None:
    """4543186544: the declared reason is checked, not only that the read was Unreadable."""
    target = dataclasses.replace(
        _target(_UnreadableForTheWrongReason),
        unreadable_when_recorded={"grade_item": UnreadableReason.PARTIAL.value},
    )
    with pytest.raises(AssertionError, match="declared partial"):
        await kit.check_recorded_reads(target)
    right = dataclasses.replace(
        target, unreadable_when_recorded={"grade_item": UnreadableReason.TIMEOUT.value}
    )
    await kit.check_recorded_reads(right)


class _InventsASetupResult(FakeLmsProvider):
    async def service_account_check(self, site, info, manifest):  # type: ignore[no-untyped-def,override]
        items = await super().service_account_check(site, info, manifest)
        return tuple(dataclasses.replace(i, result="OKAY") for i in items)


async def test_an_unknown_setup_result_is_red() -> None:
    """4543186570."""
    with pytest.raises(AssertionError, match="OKAY"):
        await kit.check_setup_items(_target(_InventsASetupResult))


class _MisspellsAState(FakeLmsProvider):
    async def probe_capabilities(self, site, manifest, flags):  # type: ignore[no-untyped-def,override]
        res = await super().probe_capabilities(site, manifest, flags)
        return tuple(dataclasses.replace(r, state="readble") for r in res)


class _ReadableWithAReason(FakeLmsProvider):
    async def probe_capabilities(self, site, manifest, flags):  # type: ignore[no-untyped-def,override]
        res = await super().probe_capabilities(site, manifest, flags)
        return tuple(dataclasses.replace(r, reason_code="timeout") for r in res)


@pytest.mark.parametrize("cls", [_MisspellsAState, _ReadableWithAReason])
async def test_an_invalid_capability_state_or_reason_is_red(
    cls: type[FakeLmsProvider],
) -> None:
    """4543186579."""
    with pytest.raises(AssertionError, match="capability"):
        await kit.check_capabilities(_target(cls))


class _Counted(FakeLmsProvider):
    built: list[FakeLmsProvider] = []

    def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **kw)
        _Counted.built.append(self)


async def test_every_provider_a_check_builds_is_closed_even_on_failure() -> None:
    """4543186536: a provider backed by an async client must not outlive its check."""
    _Counted.built.clear()
    target = _target(_Counted)
    await kit.check_recorded_reads(target)
    await kit.check_re_resolution(target)
    await kit.check_no_canary(target)
    await kit.check_setup_items(target)
    await kit.check_capabilities(target)
    for mode in kit.FAULT_MODES:
        await kit.check_fault_mode(target, mode)
    failing = dataclasses.replace(target, flags=("not_requested",), canaries=CANARIES)
    with pytest.raises(AssertionError):
        await kit.check_setup_items(
            dataclasses.replace(failing, factory=lambda m: _ForgetsAnItemCounted())
        )
    assert _Counted.built and all(p.closed for p in _Counted.built)


class _ProbeLogsItsCredential(FakeLmsProvider):
    async def probe_capabilities(self, site, manifest, flags):  # type: ignore[no-untyped-def,override]
        structlog.get_logger("vektra_lms_fake").info(f"probe {self.secret}")
        return await super().probe_capabilities(site, manifest, flags)


class _StartupLogsItsCredential(FakeLmsProvider):
    def startup_problems(self, development):  # type: ignore[no-untyped-def,override]
        if (
            not development
        ):  # only on the production path: both values must be exercised
            structlog.get_logger("vektra_lms_fake").info(f"startup {self.secret}")
        return super().startup_problems(development)


@pytest.mark.parametrize("cls", [_ProbeLogsItsCredential, _StartupLogsItsCredential])
async def test_a_credential_logged_by_a_probe_or_at_startup_is_red(
    cls: type[FakeLmsProvider],
) -> None:
    """CodeRabbit 4544262249: the log check covered the reads and the setup check only."""
    with pytest.raises(AssertionError, match="credential"):
        await kit.check_no_secret_in_logs(_target(cls))


class _ForgetsAnItemCounted(_Counted):
    async def service_account_check(self, site, info, manifest):  # type: ignore[no-untyped-def,override]
        return (await super().service_account_check(site, info, manifest))[1:]


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


def test_the_provider_re_export_is_the_same_object() -> None:
    """The Moodle readers' ``result`` module re-exports the shared outcome types: one class, not
    two, or an isinstance check across the boundary would silently fail. The vertical's own
    re-export (``vektra_assess.readable``) is gone (plan 2a-2b.4)."""
    import importlib.util

    from vektra_assess.moodle import result

    assert importlib.util.find_spec("vektra_assess.readable") is None
    assert result.Readable is Readable and result.Unreadable is Unreadable
    assert result.UnreadableReason is UnreadableReason
