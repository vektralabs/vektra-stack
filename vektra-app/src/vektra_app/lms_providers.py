"""Discovery of the LMS provider by entry point (ADR-0033 D2, plan task 2a-1.5).

``vektra_app`` does not name a provider. A provider distribution declares an entry point in the
group ``vektra.lms_providers`` (``<name> = <module>:<factory>``); the deployment names the one it
uses with ``VEKTRA_ASSESS_LMS_PROVIDER``. Two steps, so nothing of the provider runs before the
process is known to be safe (review of #207, findings 1 and 7):

- ``find_lms_provider`` resolves the name and loads the entry-point object, without calling it.
  The object carries ``REQUIRED_SECRETS`` (``declared_secrets``): the worker's key matrix checks
  those secrets before the provider is built, and the API refuses an environment variable
  carrying one (``installed_provider_secrets``), without building anything either;
- ``build_lms_provider`` calls the factory with one keyword argument, ``development`` (the
  composition root's resolved flag; a provider never re-derives it), and checks what came back:
  the Protocol, the test-double marker, its name, and that its ``required_secrets`` are the
  declared ones. A ``ProviderConfigurationError`` (an invalid provider setting) is surfaced
  with its own detail, which names the variable and the rule, never the value.

``load_lms_provider`` is both steps.

Every failure is a ``StartupValidationError`` that names the setting and lists the names that
are installed, so the operator sees what to set instead. A test double is refused before it is
built when its factory carries the marker; anything built and then refused is closed. The assess
worker loads its one provider through here at startup (``assess_worker._load_lms``).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from vektra_shared.lms import (
    REQUIRED_SECRETS_ATTRIBUTE,
    LmsProvider,
    ProviderConfigurationError,
)
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
    "build_lms_provider",
    "declared_secrets",
    "find_lms_provider",
    "installed_provider_secrets",
    "load_lms_provider",
]


class _EntryPoint(Protocol):
    name: str

    def load(self) -> Any: ...


def _installed() -> Iterable[_EntryPoint]:
    return importlib.metadata.entry_points(group=GROUP)


def _entry_points(
    entry_points: Callable[[], Iterable[_EntryPoint]] | None,
) -> list[_EntryPoint]:
    # read at call time, so a test can stand in for the installed distributions
    return list((entry_points or _installed)())


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
    try:  # the lookup too: a property can run code, and raise
        aclose = getattr(built, "aclose", None)
        if callable(aclose):
            await aclose()
    except Exception:  # the refusal is what the operator needs to see
        pass


def _load_entry(name: str, ep: _EntryPoint) -> Any:
    try:
        return ep.load()
    except Exception as exc:
        # only the type: the provider's own error text may carry configuration values
        raise _refuse(
            f"the LMS provider {name} failed to load ({type(exc).__name__})",
            "Check that the provider's distribution is installed completely.",
        ) from None


def declared_secrets(name: str, factory: Any) -> tuple[str, ...]:
    """The secrets ``factory``'s provider will declare, read without calling it. A factory with
    no declaration declares none; one that cannot be read, or is not a tuple of names, is a
    refusal (it is what the API and the key matrix rely on)."""
    try:
        raw = getattr(factory, REQUIRED_SECRETS_ATTRIBUTE, ())
    except Exception as exc:
        raise _refuse(
            f"the LMS provider {name} has an unreadable {REQUIRED_SECRETS_ATTRIBUTE} "
            f"({type(exc).__name__})",
            "Fix the provider: its entry point declares its secrets as a tuple of names.",
        ) from None
    if not isinstance(raw, tuple) or not all(isinstance(s, str) for s in raw):
        raise _refuse(
            f"the LMS provider {name} declares {REQUIRED_SECRETS_ATTRIBUTE} as "
            f"{type(raw).__name__}, not a tuple of names",
            "Fix the provider: its entry point declares its secrets as a tuple of names.",
        )
    return raw


def installed_provider_secrets(
    entry_points: Callable[[], Iterable[_EntryPoint]] | None = None,
) -> tuple[str, ...]:
    """Every secret an installed provider declares, from the entry points alone (no factory is
    called, no provider built): what the API and the parser must never hold, in a file or in
    the environment, whichever provider the deployment names (ARCH-066)."""
    found: dict[str, None] = {}
    for ep in _entry_points(entry_points):
        found.update(dict.fromkeys(declared_secrets(ep.name, _load_entry(ep.name, ep))))
    return tuple(found)


def find_lms_provider(
    name: str | None,
    *,
    entry_points: Callable[[], Iterable[_EntryPoint]] | None = None,
    allow_test_doubles: bool = False,
) -> Any:
    """The entry-point object registered as ``name``, loaded but not called."""
    eps = _entry_points(entry_points)
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
    factory = _load_entry(name, matches[0])
    if _is_test_double(factory) and not allow_test_doubles:
        raise _test_double(name)  # refused before anything is built
    return factory


def _test_double(name: str) -> StartupValidationError:
    return _refuse(
        f"the LMS provider {name} is a test double",
        f"Set {SETTING} to a real provider; test doubles load only in development tests.",
    )


async def build_lms_provider(
    name: str,
    factory: Any,
    *,
    development: bool,
    allow_test_doubles: bool = False,
) -> LmsProvider:
    """``factory(development=...)``, checked; anything built and then refused is closed."""
    declared = declared_secrets(name, factory)
    test_double = _test_double(name)
    try:
        provider = factory(development=development)
    except ProviderConfigurationError as exc:
        # the provider's own explanation: the variables and the rules, never a value
        raise _refuse(exc.detail, exc.remediation) from None
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
    else:
        # the Protocol check does not call the property; reading it can raise
        try:
            own_name = provider.name
        except Exception as exc:
            own_name = None
            problem = _refuse(
                f"the LMS provider {name} failed to report its name ({type(exc).__name__})",
                "Fix the provider: its name property must return the entry point name.",
            )
        if problem is None and own_name != name:
            problem = _refuse(
                f"the entry point {name} built a provider that calls itself {own_name}",
                "The entry point name and the provider's name must agree (it is the "
                "registry key).",
            )
        if problem is None:
            try:
                built_secrets: object = tuple(provider.required_secrets)
            except Exception as exc:
                built_secrets = f"<unreadable: {type(exc).__name__}>"
            if built_secrets != declared:
                problem = _refuse(
                    f"the LMS provider {name} declares {REQUIRED_SECRETS_ATTRIBUTE} "
                    f"{list(declared)} on its entry point but required_secrets "
                    f"{list(built_secrets) if isinstance(built_secrets, tuple) else built_secrets}",
                    "Fix the provider: the two declarations must be the same names (the key "
                    "matrix checks the static one before the provider is built).",
                )
    if problem is not None:
        await _close(provider)
        raise problem
    checked: LmsProvider = provider  # isinstance-checked above
    return checked


async def load_lms_provider(
    name: str | None,
    *,
    development: bool,
    entry_points: Callable[[], Iterable[_EntryPoint]] | None = None,
    allow_test_doubles: bool = False,
) -> LmsProvider:
    """The provider registered as ``name`` in ``vektra.lms_providers``, found, built, checked."""
    factory = find_lms_provider(
        name, entry_points=entry_points, allow_test_doubles=allow_test_doubles
    )
    assert name  # find_lms_provider refused an unset one
    return await build_lms_provider(
        name, factory, development=development, allow_test_doubles=allow_test_doubles
    )
