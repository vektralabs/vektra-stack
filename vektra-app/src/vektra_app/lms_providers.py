"""Discovery of the LMS provider by entry point (ADR-0033 D2, plan task 2a-1.5).

``vektra_app`` does not name a provider. A provider distribution declares an entry point in the
group ``vektra.lms_providers`` (``<name> = <module>:<factory>``); the deployment names the one it
uses with ``VEKTRA_ASSESS_LMS_PROVIDER``. ``load_lms_provider`` resolves that name, calls the
factory with no argument (a provider reads its own settings) and checks what came back.

Every failure is a ``StartupValidationError`` that names the setting and lists the names that
are installed, so the operator sees what to set instead. Not wired yet: the worker composes its
LMS client directly until plan task 2a-4b.5.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from vektra_shared.lms import LmsProvider
from vektra_shared.startup import StartupValidationError

GROUP = "vektra.lms_providers"
SETTING = "VEKTRA_ASSESS_LMS_PROVIDER"
STEP = "lms_provider"
#: same marker as ``ProviderRegistry`` refuses (a test double is never a production provider)
_TEST_DOUBLE_MARKER = "__vektra_test_double__"


class _EntryPoint(Protocol):
    name: str

    def load(self) -> Any: ...


def _installed() -> Iterable[_EntryPoint]:
    return importlib.metadata.entry_points(group=GROUP)


def available_lms_providers(
    entry_points: Callable[[], Iterable[_EntryPoint]] = _installed,
) -> tuple[str, ...]:
    return tuple(sorted({ep.name for ep in entry_points()}))


def _refuse(detail: str, remediation: str) -> StartupValidationError:
    return StartupValidationError(step=STEP, detail=detail, remediation=remediation)


def load_lms_provider(
    name: str | None,
    *,
    entry_points: Callable[[], Iterable[_EntryPoint]] = _installed,
    allow_test_doubles: bool = False,
) -> LmsProvider:
    """The provider registered as ``name`` in ``vektra.lms_providers``, built and checked."""
    eps = list(entry_points())
    names = sorted({ep.name for ep in eps})
    listed = ", ".join(names) if names else "none installed"
    install = (
        f"Install the provider's distribution (it declares the entry point group {GROUP}) "
        f"and set {SETTING} to its name."
    )
    if not name:
        raise _refuse(f"{SETTING} is unset; available providers: {listed}", install)
    matches = [ep for ep in eps if ep.name == name]
    if not matches:
        raise _refuse(
            f"{SETTING}={name} names no installed provider; available providers: {listed}",
            install,
        )
    if len(matches) > 1:
        raise _refuse(
            f"{SETTING}={name} is declared by {len(matches)} installed distributions",
            "Uninstall all but one distribution that declares this provider name.",
        )
    try:
        factory = matches[0].load()
        provider = factory()
    except Exception as exc:
        # only the type: the provider's own error text may carry configuration values
        raise _refuse(
            f"the LMS provider {name} failed to load ({type(exc).__name__})",
            "Check the provider's own settings and that its distribution is installed "
            "completely.",
        ) from None
    if not isinstance(provider, LmsProvider):
        raise _refuse(
            f"the entry point {name} returned {type(provider).__name__}, which does not "
            "implement LmsProvider",
            "Use a provider built for this platform version (its conformance run names it).",
        )
    if getattr(type(provider), _TEST_DOUBLE_MARKER, False) and not allow_test_doubles:
        raise _refuse(
            f"the LMS provider {name} is a test double",
            f"Set {SETTING} to a real provider; test doubles load only in development tests.",
        )
    if provider.name != name:
        raise _refuse(
            f"the entry point {name} built a provider that calls itself {provider.name}",
            "The entry point name and the provider's name must agree (it is the registry key).",
        )
    return provider
