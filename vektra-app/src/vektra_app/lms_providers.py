"""Discovery of the LMS provider by entry point (ADR-0033 D2, plan task 2a-1.5).

``vektra_app`` does not name a provider. A provider distribution declares an entry point in the
group ``vektra.lms_providers`` (``<name> = <module>:<factory>``); the deployment names the one it
uses with ``VEKTRA_ASSESS_LMS_PROVIDER``. ``load_lms_provider`` resolves that name, calls the
factory with no argument (a provider reads its own settings) and checks what came back.

Every failure is a ``StartupValidationError`` that names the setting and lists the names that
are installed, so the operator sees what to set instead. A test double is refused before it is
built when its factory carries the marker; anything built and then refused is closed. The assess
worker loads its one provider through here at startup (``assess_worker._load_lms``).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from vektra_shared.lms import LmsProvider
from vektra_shared.registry import TEST_DOUBLE_MARKER
from vektra_shared.startup import StartupValidationError

GROUP = "vektra.lms_providers"
SETTING = "VEKTRA_ASSESS_LMS_PROVIDER"
STEP = "lms_provider"

__all__ = [
    "GROUP",
    "SETTING",
    "TEST_DOUBLE_MARKER",
    "available_lms_providers",
    "load_lms_provider",
]


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


def _is_test_double(obj: Any) -> bool:
    """The marker on a class, on an instance's class, or on a factory function."""
    holder = obj if isinstance(obj, type) or callable(obj) else type(obj)
    return bool(
        getattr(holder, TEST_DOUBLE_MARKER, False)
        or getattr(type(obj), TEST_DOUBLE_MARKER, False)
    )


async def _close(built: Any) -> None:
    """Best effort: a refused provider must not keep its client, pool or thread alive."""
    aclose = getattr(built, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:  # the refusal is what the operator needs to see
            pass


async def load_lms_provider(
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
    test_double = _refuse(
        f"the LMS provider {name} is a test double",
        f"Set {SETTING} to a real provider; test doubles load only in development tests.",
    )
    try:
        factory = matches[0].load()
    except Exception as exc:
        # only the type: the provider's own error text may carry configuration values
        raise _refuse(
            f"the LMS provider {name} failed to load ({type(exc).__name__})",
            "Check that the provider's distribution is installed completely.",
        ) from None
    if _is_test_double(factory) and not allow_test_doubles:
        raise test_double  # refused before anything is built
    try:
        provider = factory()
    except Exception as exc:
        raise _refuse(
            f"the LMS provider {name} failed to load ({type(exc).__name__})",
            "Check the provider's own settings and that its distribution is installed "
            "completely.",
        ) from None
    problem: StartupValidationError | None = None
    if not isinstance(provider, LmsProvider):
        problem = _refuse(
            f"the entry point {name} returned {type(provider).__name__}, which does not "
            "implement LmsProvider",
            "Use a provider built for this platform version (its conformance run names it).",
        )
    elif _is_test_double(provider) and not allow_test_doubles:
        problem = test_double
    elif provider.name != name:
        problem = _refuse(
            f"the entry point {name} built a provider that calls itself {provider.name}",
            "The entry point name and the provider's name must agree (it is the registry key).",
        )
    if problem is not None:
        await _close(provider)
        raise problem
    checked: LmsProvider = provider  # isinstance-checked above
    return checked
