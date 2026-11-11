"""The LMS provider conformance kit: the checks, run by ``suite.LmsConformanceSuite``
(ADR-0033 D10, plan task 2a-1.7).

A provider is registrable for production only with a green run against the platform version it
declares. The kit drives a provider through a ``ConformanceTarget``: a factory that builds the
provider over its **recorded transport** in a given ``TransportMode``, the ids that exist in that
recording, the canaries planted in its LMS-side forbidden fields, and the credentials it uses.

Each check is a plain function that raises ``AssertionError``, so a consumer can also run it
against a deliberately broken provider and prove that it says no (the negative controls).

The canary check walks every returned value, numbers included. ``ReviewHtml.html`` is the
teacher-visible rendering by design, so it is walked **as its own profile leaves it**: only the
text inside the profile's kept blocks, minus every dropped class (``profile_text``, a stdlib
parse of the declarative profile). A profile that keeps what the LMS hides fails the check.

The anti-vacuous guard: ``canary_witness`` must return the text of what the provider's read
paths actually consumed during the run (its recorded documents), never a list of the canaries
themselves; a canary that no read path touched proves nothing and fails the check.

Skeleton scope (2a-1). Not yet here, and owned by later tasks:

- the exact LLM request bytes of a full capture (the vertical's canary sinks, 2a-2);
- the run on the second provider (the recorded fixtures of the existing client) is task 2a-2a.1.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from html.parser import HTMLParser
from typing import Any

import structlog

from vektra_shared.lms import (
    NOT_IN_USER_COURSES,
    SETUP_OK,
    CapabilityResult,
    LmsProvider,
    LmsSite,
    Readable,
    ReviewHtml,
    SetupItem,
    StaffRole,
    StartupProblem,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)

#: the setup-item name rule the runner enforces (ADR-0033 D3)
ITEM_NAME = re.compile(r"[a-z][a-z0-9_]{1,48}")


class TransportMode(StrEnum):
    """How the provider's recorded transport behaves for one run."""

    RECORDED = "recorded"  # the recording answers as recorded
    UNKNOWN_SHAPE = (
        "unknown_shape"  # every answer has a shape the provider does not know
    )
    TIMEOUT = "timeout"  # every call times out
    BREAKER_OPEN = "breaker_open"  # the provider's breaker is open for the site
    RAISING = "raising"  # the transport raises an arbitrary exception

    @property
    def expected_reason(self) -> UnreadableReason | None:
        return {
            TransportMode.UNKNOWN_SHAPE: UnreadableReason.UNKNOWN_SHAPE,
            TransportMode.TIMEOUT: UnreadableReason.TIMEOUT,
            TransportMode.BREAKER_OPEN: UnreadableReason.BREAKER_OPEN,
        }.get(self)


FAULT_MODES = tuple(m for m in TransportMode if m is not TransportMode.RECORDED)


@dataclass(frozen=True)
class ConformanceIds:
    """Ids that exist in the target's recording. The ``foreign_*`` pair is an activity in a
    course the user is NOT enrolled in (the re-resolution refusal)."""

    user_id: int
    course_id: int
    quiz_activity_id: int
    quiz_id: int
    assignment_activity_id: int
    assignment_id: int
    attempt_id: int
    definition_id: int
    submission_id: int
    file_ref: SubmissionFileRef
    foreign_activity_id: int
    foreign_course_id: int


@dataclass(frozen=True)
class ConformanceTarget:
    name: str
    #: builds a fresh provider over the recorded transport, behaving as the mode says
    factory: Callable[[TransportMode], LmsProvider]
    site: LmsSite
    ids: ConformanceIds
    #: planted in LMS-side forbidden fields of the recording (right answers, hidden feedback,
    #: unreleased grades...): never in a returned value
    canaries: tuple[str, ...]
    #: called after a recorded run with the provider that ran: the text of the recorded documents
    #: its read paths consumed in that run. Proves the canaries were in what it read; a list of
    #: the canaries themselves would make the guard pass by construction
    canary_witness: Callable[[LmsProvider], str]
    #: credentials the transport uses: never in a log line, at any level
    secrets: tuple[str, ...]
    #: capability flags to probe (the vertical's ``capabilities.FLAGS``)
    flags: tuple[str, ...]
    manifest: Mapping[str, Any] | None = None
    #: data members that are Unreadable even on the recording, with the reason: declared, so a
    #: provider cannot pass the kit by answering Unreadable everywhere
    unreadable_when_recorded: Mapping[str, str] = field(default_factory=dict)


# --- the members ---------------------------------------------------------------------------

Invocation = Callable[[LmsProvider, ConformanceTarget], Awaitable[Any]]


def _inv() -> dict[str, Invocation]:
    def i(t: ConformanceTarget) -> ConformanceIds:
        return t.ids

    return {
        "site_info": lambda p, t: p.site_info(t.site),
        "resolve_activity": lambda p, t: p.resolve_activity(
            t.site,
            user_id=i(t).user_id,
            activity_id=i(t).quiz_activity_id,
            kind="quiz",
            course_hint=None,
        ),
        "resolve_in_course": lambda p, t: p.resolve_in_course(
            t.site,
            activity_id=i(t).assignment_activity_id,
            kind="assign",
            course_id=i(t).course_id,
        ),
        "enrolment": lambda p, t: p.enrolment(t.site, i(t).course_id, i(t).user_id),
        "course_staff": lambda p, t: p.course_staff(
            t.site, i(t).course_id, StaffRole.GRADER
        ),
        "quiz_settings": lambda p, t: p.quiz_settings(
            t.site, i(t).course_id, i(t).quiz_id
        ),
        "review_options": lambda p, t: p.review_options(
            t.site, i(t).quiz_id, i(t).user_id
        ),
        "attempts": lambda p, t: p.attempts(t.site, i(t).quiz_id, i(t).user_id),
        "attempt_review": lambda p, t: p.attempt_review(t.site, i(t).attempt_id),
        "assignment_settings": lambda p, t: p.assignment_settings(
            t.site, i(t).course_id, i(t).assignment_id
        ),
        "assignment_dates": lambda p, t: p.assignment_dates(
            t.site, i(t).assignment_id, i(t).user_id
        ),
        "submission": lambda p, t: p.submission(
            t.site, i(t).assignment_id, i(t).user_id
        ),
        "submission_file": lambda p, t: p.submission_file(
            t.site,
            i(t).assignment_id,
            i(t).file_ref,
            i(t).user_id,
            i(t).submission_id,
        ),
        "assignment_release": lambda p, t: p.assignment_release(
            t.site, i(t).course_id, i(t).assignment_id, i(t).user_id
        ),
        "grade_item": lambda p, t: p.grade_item(
            t.site, i(t).course_id, i(t).user_id, i(t).quiz_activity_id
        ),
        "grading_definition": lambda p, t: p.grading_definition(
            t.site, i(t).assignment_activity_id
        ),
        "rubric_fillings": lambda p, t: p.rubric_fillings(t.site, i(t).definition_id),
    }


#: every member that returns a ``Reading``, with how the kit calls it
READING_MEMBERS: dict[str, Invocation] = _inv()
#: the members checked by dedicated checks (properties, lifecycle, setup check, probes)
OTHER_MEMBERS = frozenset(
    {
        "name",
        "setup_items",
        "startup_problems",
        "aclose",
        "service_account_check",
        "probe_capabilities",
    }
)


def protocol_members() -> frozenset[str]:
    return frozenset(LmsProvider.__protocol_attrs__)  # type: ignore[attr-defined]


async def read_all(provider: LmsProvider, target: ConformanceTarget) -> dict[str, Any]:
    """Every Reading member once. An exception escaping a member fails the check, named."""
    out: dict[str, Any] = {}
    for member, call in READING_MEMBERS.items():
        try:
            out[member] = await call(provider, target)
        except Exception as exc:
            raise AssertionError(
                f"{target.name}.{member} let {type(exc).__name__} escape (Reading discipline)"
            ) from exc
    return out


class _ProfileText(HTMLParser):
    """The text a declarative ``HtmlExtractionProfile`` keeps: inside a kept block, outside
    every dropped class. A reference reading of the profile, for the canary check only; the
    sidecar is the production extractor (ADR-0033 D4)."""

    _VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
        }
    )

    def __init__(self, keep: frozenset[str], drop: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._keep, self._drop = keep, drop
        self._stack: list[tuple[bool, bool]] = []  # (opens a kept block, opens a drop)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            return
        classes = set((dict(attrs).get("class") or "").split())
        self._stack.append((bool(classes & self._keep), bool(classes & self._drop)))

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._VOID and self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        kept = any(k for k, _ in self._stack)
        dropped = any(d for _, d in self._stack)
        if kept and not dropped:
            self.out.append(data)


def profile_text(review: ReviewHtml) -> str:
    parser = _ProfileText(
        frozenset(review.profile.keep.values()), frozenset(review.profile.drop_classes)
    )
    parser.feed(review.html)
    parser.close()
    return " ".join(parser.out)


def walk_text(value: Any) -> Iterator[str]:
    """Every string, bytes and number inside a returned value; review HTML as its own profile
    leaves it (see the module docstring)."""
    if isinstance(value, ReviewHtml):
        yield profile_text(value)
        yield from walk_text(value.profile)
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, bytes):
        yield value.decode("utf-8", errors="replace")
    elif isinstance(value, Enum):
        yield str(value.value)
    elif isinstance(
        value, (int, float)
    ):  # an unreleased grade is a number (bool included)
        yield repr(value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from walk_text(getattr(value, f.name))
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield from walk_text(k)
            yield from walk_text(v)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for v in value:
            yield from walk_text(v)


@contextmanager
def captured_logs() -> Iterator[io.StringIO]:
    """Every log line, both stacks, at DEBUG, through the production log-safety chain."""
    from vektra_shared.log_safety import configure_assess_worker_logging

    buf = io.StringIO()
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    configure_assess_worker_logging(buf, level=logging.DEBUG)
    try:
        yield buf
    finally:
        structlog.reset_defaults()
        root.handlers[:] = before[0]
        root.setLevel(before[1])


# --- the checks ----------------------------------------------------------------------------


def check_protocol(provider: LmsProvider) -> None:
    assert isinstance(provider, LmsProvider), (
        f"{type(provider).__name__} does not implement LmsProvider"
    )
    assert isinstance(provider.name, str) and provider.name, "a provider has a name"
    names = provider.setup_items
    assert isinstance(names, tuple), "setup_items is a tuple"
    assert len(set(names)) == len(names), "setup item names are unique"
    bad = [n for n in names if not ITEM_NAME.fullmatch(n)]
    assert not bad, f"setup item names break [a-z][a-z0-9_]{{1,48}}: {bad}"


async def check_recorded_reads(target: ConformanceTarget) -> None:
    provider = target.factory(TransportMode.RECORDED)
    results = await read_all(provider, target)
    assert set(results) == set(READING_MEMBERS), "every Reading member was called"
    for member, res in results.items():
        assert isinstance(res, (Readable, Unreadable)), (
            f"{member} returned {type(res).__name__}, not a Reading"
        )
        if member in target.unreadable_when_recorded:
            assert isinstance(res, Unreadable), (
                f"{member} is declared Unreadable on the recording but answered"
            )
        else:
            assert isinstance(res, Readable), (
                f"{member} is Unreadable on its own recording ({res}); declare it in "
                "unreadable_when_recorded with the reason, or fix the provider"
            )


async def check_fault_mode(target: ConformanceTarget, mode: TransportMode) -> None:
    provider = target.factory(mode)
    results = await read_all(provider, target)
    for member, res in results.items():
        assert isinstance(res, Unreadable), f"{member} under {mode} answered {res!r}"
        if mode.expected_reason is not None:
            assert res.reason is mode.expected_reason, (
                f"{member} under {mode} is {res.reason}, expected {mode.expected_reason}"
            )
    info = await provider.site_info(target.site)
    items = await provider.service_account_check(target.site, info, target.manifest)
    assert all(isinstance(i, SetupItem) for i in items)
    assert not [i.name for i in items if i.result == SETUP_OK], (
        f"setup items OK under {mode}: a fault never proves an item"
    )
    flags = await provider.probe_capabilities(
        target.site, target.manifest, target.flags
    )
    assert all(isinstance(f, CapabilityResult) for f in flags)
    assert not [f.flag for f in flags if f.state == "readable"], (
        f"flags readable under {mode}"
    )


async def check_re_resolution(target: ConformanceTarget) -> None:
    provider = target.factory(TransportMode.RECORDED)
    ids = target.ids
    own = await provider.resolve_activity(
        target.site,
        user_id=ids.user_id,
        activity_id=ids.quiz_activity_id,
        kind="quiz",
        course_hint=ids.foreign_course_id,  # a wrong hint is only a hint
    )
    assert isinstance(own, Readable) and own.value.course_id == ids.course_id, (
        f"the user's own activity resolves to its own course whatever the hint: {own!r}"
    )
    foreign = await provider.resolve_activity(
        target.site,
        user_id=ids.user_id,
        activity_id=ids.foreign_activity_id,
        kind="quiz",
        course_hint=ids.foreign_course_id,
    )
    assert isinstance(foreign, Unreadable), (
        f"an activity outside the user's courses resolved: {foreign!r} (REQ-095)"
    )
    assert foreign.detail_code == NOT_IN_USER_COURSES, foreign


async def check_no_canary(target: ConformanceTarget) -> None:
    assert target.canaries, "a conformance target plants at least one canary"
    provider = target.factory(TransportMode.RECORDED)
    results = await read_all(provider, target)
    witness = target.canary_witness(provider)
    missing = [c for c in target.canaries if c not in witness]
    assert not missing, (
        f"canaries no read path consumed in this run (vacuous check): {missing}"
    )
    for member, res in results.items():
        text = "\n".join(walk_text(res))
        hits = [c for c in target.canaries if c in text]
        assert not hits, f"{member} returned forbidden content: {hits}"


async def check_no_secret_in_logs(target: ConformanceTarget) -> None:
    assert target.secrets, "a conformance target names the credentials it uses"
    with captured_logs() as buf:
        for mode in TransportMode:
            provider = target.factory(mode)
            await read_all(provider, target)
            info = await provider.site_info(target.site)
            await provider.service_account_check(target.site, info, target.manifest)
            await provider.aclose()
        for h in logging.getLogger().handlers:
            h.flush()
    text = buf.getvalue()
    hits = [s for s in target.secrets if s in text]
    assert not hits, f"{len(hits)} credential(s) in the log output"


async def check_setup_items(target: ConformanceTarget) -> None:
    provider = target.factory(TransportMode.RECORDED)
    info = await provider.site_info(target.site)
    items = await provider.service_account_check(target.site, info, target.manifest)
    returned = [i.name for i in items]
    assert len(set(returned)) == len(returned), f"an item returned twice: {returned}"
    declared = set(provider.setup_items)
    assert set(returned) == declared, (
        f"declared but not returned: {sorted(declared - set(returned))}; "
        f"returned but not declared: {sorted(set(returned) - declared)}"
    )


async def check_capabilities(target: ConformanceTarget) -> None:
    provider = target.factory(TransportMode.RECORDED)
    flags = await provider.probe_capabilities(
        target.site, target.manifest, target.flags
    )
    assert sorted(f.flag for f in flags) == sorted(target.flags), (
        "exactly one result per requested flag"
    )


async def check_lifecycle(target: ConformanceTarget) -> None:
    provider = target.factory(TransportMode.RECORDED)
    for development in (True, False):
        problems = provider.startup_problems(development)
        assert isinstance(problems, tuple)
        assert all(isinstance(p, StartupProblem) for p in problems)
    await provider.aclose()
