"""The ``LmsProvider`` Protocol: the 11th Protocol of the platform (ADR-0033 D2, ARCH-039).

A vertical that reads an LMS depends on this Protocol only. The vertical keeps the authority:
it decides, it re-resolves every id it is given (REQ-095), and it schedules. A provider is the
transport: it looks up, reads and normalises into the neutral types of
``vektra_shared.lms.types``. It is discovered by entry point (group ``vektra.lms_providers``)
and registered under the ``lms`` category of ``ProviderRegistry``.

Invariants every provider keeps, proved by the conformance kit (ADR-0033 D10):

1. every read returns a ``Reading``: an exception, an unknown shape, a timeout or an open
   breaker all come back as ``Unreadable``. The vertical also wraps each call and maps an
   escaped exception to ``Unreadable(UNKNOWN_SHAPE, PROVIDER_ERROR)``;
2. no value carries a solution, a right answer, hidden feedback or an unreleased grade (the
   types have no slot for one); review HTML travels with the profile that cleans it;
3. ``resolve_activity`` for an activity outside the user's courses returns
   ``Unreadable(..., NOT_IN_USER_COURSES)``, never the hinted course;
4. no credential appears in a log line at any level. A provider's loggers register their
   prefix with ``vektra_shared.log_safety.register_strict_logger_prefix``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from vektra_shared.lms.types import (
    ActivityKind,
    ActivityRef,
    AssignmentDates,
    AssignmentRelease,
    AssignmentSettings,
    AttemptReview,
    AttemptSummary,
    Enrolment,
    GradeItem,
    GradingDefinition,
    LmsSite,
    LmsSiteInfo,
    QuizSettings,
    Reading,
    ReviewOptions,
    RubricFilling,
    StaffMember,
    StaffRole,
    Submission,
    SubmissionFileRef,
)

#: results of one setup item (REQ-079); the strings are stored and returned by the setup report
SETUP_OK = "OK"
SETUP_MISSING = "MISSING"
SETUP_UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class SetupItem:
    """One service-account item of the setup check, as decided by the provider."""

    name: str
    result: str  # SETUP_OK | SETUP_MISSING | SETUP_UNVERIFIABLE
    reason_code: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "result": self.result,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class CapabilityResult:
    """One probed capability flag (ARCH-070). The flag names are Vektra's (``capabilities.FLAGS``
    in the vertical); the provider only probes them."""

    flag: str
    state: str  # readable | partial | unreadable
    reason_code: str | None  # an ``UnreadableReason`` value; None when readable
    evidence_hash: str


@dataclass(frozen=True)
class StartupProblem:
    """A provider setting that must stop the process before it starts. The composition root
    turns it into a ``StartupValidationError``; the value itself is never included."""

    setting: str
    detail: str
    remediation: str


class ProviderConfigurationError(Exception):
    """Raised by a provider's entry-point factory when its own settings are invalid. The loader
    surfaces ``detail`` and ``remediation`` as they are, so they name the variables and the rule
    they broke, never a value (``vektra_shared.settings_errors``)."""

    def __init__(self, detail: str, remediation: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation


#: the attribute of a provider's entry-point object that lists the secrets the provider will
#: declare (``LmsProvider.required_secrets``), readable without building it: the API and the
#: parser refuse an environment variable carrying one of them (ARCH-066)
REQUIRED_SECRETS_ATTRIBUTE = "REQUIRED_SECRETS"
#: the bound of ``LmsProvider.call_deadline_s``
MAX_CALL_DEADLINE_S = 600.0


@runtime_checkable
class LmsProvider(Protocol):
    """Read access to one LMS product, for every site of a deployment (25 members).

    The entry point is a factory called with one keyword argument, ``development`` (the
    composition root's resolved flag), and carrying ``REQUIRED_SECRETS`` (the value
    ``required_secrets`` returns), so the secret names are known before anything is built."""

    @property
    def name(self) -> str:
        """Registry key, metrics and log label."""
        ...

    @property
    def setup_items(self) -> tuple[str, ...]:
        """The service-account items ``service_account_check`` returns (ADR-0033 D3)."""
        ...

    @property
    def required_secrets(self) -> tuple[str, ...]:
        """The worker secrets (key-matrix names) the provider reads, its LMS credentials among
        them: the composition binds the provider's jobs only when every one was loaded."""
        ...

    @property
    def call_deadline_s(self) -> float:
        """The longest one LMS call may take, transport included (``0 < x <= 600``): the
        vertical budgets its jobs that make sequential calls from it."""
        ...

    # --- lifecycle --------------------------------------------------------------------------

    def startup_problems(self, development: bool) -> tuple[StartupProblem, ...]:
        """What must stop the process. ``development`` is the composition root's resolved flag
        (an explicit ``VEKTRA_ENV=development``), never the raw variable: a provider must not
        count the platform default as development."""
        ...

    async def aclose(self) -> None: ...

    # --- site and service account (REQ-079, ARCH-070) ---------------------------------------

    async def site_info(self, site: LmsSite) -> Reading[LmsSiteInfo]: ...

    async def service_account_check(
        self,
        site: LmsSite,
        info: Reading[LmsSiteInfo],
        manifest: Mapping[str, Any] | None,
    ) -> tuple[SetupItem, ...]: ...

    async def probe_capabilities(
        self, site: LmsSite, manifest: Mapping[str, Any] | None, flags: Sequence[str]
    ) -> tuple[CapabilityResult, ...]: ...

    # --- resolution (REQ-095): Vektra decides to re-resolve, the provider looks up ----------

    async def resolve_activity(
        self,
        site: LmsSite,
        *,
        user_id: int,
        activity_id: int,
        kind: ActivityKind,
        course_hint: int | None,
    ) -> Reading[ActivityRef]: ...

    async def resolve_in_course(
        self, site: LmsSite, *, activity_id: int, kind: ActivityKind, course_id: int
    ) -> Reading[ActivityRef]: ...

    # --- people -----------------------------------------------------------------------------

    async def enrolment(
        self, site: LmsSite, course_id: int, user_id: int
    ) -> Reading[Enrolment]: ...

    async def course_staff(
        self, site: LmsSite, course_id: int, role: StaffRole
    ) -> Reading[tuple[StaffMember, ...]]: ...

    # --- quiz -------------------------------------------------------------------------------

    async def quiz_settings(
        self, site: LmsSite, course_id: int, quiz_id: int
    ) -> Reading[QuizSettings]: ...

    async def review_options(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[ReviewOptions]: ...

    async def attempts(
        self, site: LmsSite, quiz_id: int, user_id: int
    ) -> Reading[tuple[AttemptSummary, ...]]: ...

    async def attempt_review(
        self, site: LmsSite, attempt_id: int
    ) -> Reading[AttemptReview]: ...

    # --- assignment -------------------------------------------------------------------------

    async def assignment_settings(
        self, site: LmsSite, course_id: int, assignment_id: int
    ) -> Reading[AssignmentSettings]: ...

    async def assignment_dates(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[AssignmentDates]: ...

    async def submission(
        self, site: LmsSite, assignment_id: int, user_id: int
    ) -> Reading[Submission]: ...

    async def submission_file(
        self,
        site: LmsSite,
        assignment_id: int,
        ref: SubmissionFileRef,
        owner_user_id: int,
        submission_id: int,
    ) -> Reading[bytes]: ...

    async def assignment_release(
        self, site: LmsSite, course_id: int, assignment_id: int, user_id: int
    ) -> Reading[AssignmentRelease]: ...

    # --- grading ----------------------------------------------------------------------------

    async def grade_item(
        self, site: LmsSite, course_id: int, user_id: int, activity_id: int
    ) -> Reading[GradeItem]: ...

    async def grading_definition(
        self, site: LmsSite, activity_id: int
    ) -> Reading[GradingDefinition]: ...

    async def rubric_fillings(
        self, site: LmsSite, definition_id: int
    ) -> Reading[tuple[RubricFilling, ...]]: ...
