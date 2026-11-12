"""The one fail-closed wrapper around an ``LmsProvider`` (ADR-0033 D2, invariant 1).

A provider must never let an exception out; the vertical does not rely on it. The composition
root wraps the loaded provider once with ``guarded()``, and the vertical's entry points call it
again, which is a no-op on a provider already guarded. Every Protocol member has a declared
failure, applied when the provider raises:

- a read member returns ``Unreadable(UNKNOWN_SHAPE, PROVIDER_ERROR)``;
- ``service_account_check``: every declared item ``UNVERIFIABLE/check_error``;
- ``probe_capabilities``: every requested flag ``unreadable/not_probed`` (a raise proves
  nothing, so it must not move the site's release as a probed matrix would);
- ``startup_problems``: one problem, so the process does not start;
- ``aclose``: the error is logged and dropped (shutdown goes on);
- ``setup_items`` / ``required_secrets``: a declaration that cannot be read is an invalid one,
  which the setup check and the composition both refuse; an unreadable ``name`` is
  ``<unnamed>``, which matches no configured provider.

Only the Protocol is reachable through the wrapper: there is no forwarding of other attributes.

The member table is checked against the Protocol at import: a member added to ``LmsProvider``
without a declared failure is an ``ImportError``, never an unguarded path. Only the exception's
type is logged: its text may carry what the provider read or its credentials.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

import structlog

from vektra_shared.lms.protocol import (
    SETUP_UNVERIFIABLE,
    CapabilityResult,
    LmsProvider,
    SetupItem,
    StartupProblem,
)
from vektra_shared.lms.types import (
    PROVIDER_ERROR,
    LmsSite,
    LmsSiteInfo,
    Reading,
    Unreadable,
    UnreadableReason,
)

log = structlog.get_logger(__name__)

#: a declared name no validator accepts: what an unreadable declaration becomes
INVALID_DECLARATION = "!unreadable"
#: what an unreadable provider name becomes (it never matches a configured name)
UNNAMED = "<unnamed>"

#: the members that return a ``Reading``
READ_MEMBERS: tuple[str, ...] = (
    "site_info",
    "resolve_activity",
    "resolve_in_course",
    "enrolment",
    "course_staff",
    "quiz_settings",
    "review_options",
    "attempts",
    "attempt_review",
    "assignment_settings",
    "assignment_dates",
    "submission",
    "submission_file",
    "assignment_release",
    "grade_item",
    "grading_definition",
    "rubric_fillings",
)
#: the members with their own declared failure, implemented below
OTHER_MEMBERS: tuple[str, ...] = (
    "name",
    "setup_items",
    "required_secrets",
    "startup_problems",
    "aclose",
    "service_account_check",
    "probe_capabilities",
)


def protocol_members() -> frozenset[str]:
    return frozenset(LmsProvider.__protocol_attrs__)  # type: ignore[attr-defined]


def uncovered_members() -> frozenset[str]:
    """Protocol members this wrapper has no declared failure for (must be empty)."""
    return protocol_members() - set(READ_MEMBERS) - set(OTHER_MEMBERS)


def _failed(member: str, exc: BaseException) -> None:
    log.warning("lms_provider_error", member=member, error=type(exc).__name__)


def not_probed(flag: str, release: str | None) -> CapabilityResult:
    """The fail-closed result of a capability flag nobody probed (content-free evidence)."""
    reason = UnreadableReason.NOT_PROBED.value
    evidence = hashlib.sha256(f"{flag}\n{release or ''}\n{reason}".encode()).hexdigest()
    return CapabilityResult(flag, "unreadable", reason, evidence)


def _read_member(member: str) -> Callable[..., Awaitable[Reading[Any]]]:
    async def call(self: GuardedLmsProvider, *args: Any, **kwargs: Any) -> Reading[Any]:
        try:
            return cast(
                "Reading[Any]", await getattr(self.inner, member)(*args, **kwargs)
            )
        except Exception as exc:
            _failed(member, exc)
            return Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR)

    call.__name__ = member
    return call


class GuardedLmsProvider:
    """``LmsProvider`` over ``inner``: the same answers, and a declared failure instead of an
    exception. Only the Protocol is exposed: no other attribute of the inner provider is
    reachable through the wrapper (a caller that needs one has found a Protocol gap)."""

    def __init__(self, inner: LmsProvider) -> None:
        self.inner = inner

    @property
    def name(self) -> str:
        try:
            return str(self.inner.name)
        except Exception as exc:
            _failed("name", exc)
            return UNNAMED

    def _declared(self, member: str) -> tuple[str, ...]:
        try:
            value = getattr(self.inner, member)
            return tuple(value)
        except Exception as exc:
            _failed(member, exc)
            return (INVALID_DECLARATION,)

    @property
    def setup_items(self) -> tuple[str, ...]:
        return self._declared("setup_items")

    @property
    def required_secrets(self) -> tuple[str, ...]:
        return self._declared("required_secrets")

    def startup_problems(self, development: bool) -> tuple[StartupProblem, ...]:
        try:
            return tuple(self.inner.startup_problems(development))
        except Exception as exc:
            _failed("startup_problems", exc)
            return (
                StartupProblem(
                    setting="VEKTRA_ASSESS_LMS_PROVIDER",
                    detail=(
                        f"the LMS provider {self.name} named by VEKTRA_ASSESS_LMS_PROVIDER "
                        f"failed its startup check ({type(exc).__name__})"
                    ),
                    remediation="Check the provider's own settings.",
                ),
            )

    async def aclose(self) -> None:
        try:
            await self.inner.aclose()
        except Exception as exc:
            _failed("aclose", exc)

    async def service_account_check(
        self,
        site: LmsSite,
        info: Reading[LmsSiteInfo],
        manifest: Mapping[str, Any] | None,
    ) -> tuple[SetupItem, ...]:
        try:
            return tuple(await self.inner.service_account_check(site, info, manifest))
        except Exception as exc:
            _failed("service_account_check", exc)
            return tuple(
                SetupItem(n, SETUP_UNVERIFIABLE, "check_error")
                for n in self.setup_items
            )

    async def probe_capabilities(
        self, site: LmsSite, manifest: Mapping[str, Any] | None, flags: Sequence[str]
    ) -> tuple[CapabilityResult, ...]:
        try:
            return tuple(await self.inner.probe_capabilities(site, manifest, flags))
        except Exception as exc:
            _failed("probe_capabilities", exc)
            return tuple(not_probed(f, site.release) for f in flags)


for _member in READ_MEMBERS:
    setattr(GuardedLmsProvider, _member, _read_member(_member))

if (
    uncovered_members()
):  # pragma: no cover - a Protocol change without a declared failure
    raise ImportError(
        f"LmsProvider members without a declared failure: {sorted(uncovered_members())}"
    )


def guarded(provider: LmsProvider) -> LmsProvider:
    """``provider`` behind the fail-closed wrapper; a provider already wrapped is returned as is."""
    if isinstance(provider, GuardedLmsProvider):
        return cast(LmsProvider, provider)
    return cast(LmsProvider, GuardedLmsProvider(provider))


__all__ = [
    "INVALID_DECLARATION",
    "OTHER_MEMBERS",
    "READ_MEMBERS",
    "UNNAMED",
    "GuardedLmsProvider",
    "guarded",
    "not_probed",
    "uncovered_members",
]
