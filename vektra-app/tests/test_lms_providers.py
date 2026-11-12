"""Entry-point discovery of the LMS provider (plan tasks 2a-1.5 and 2a-2, ADR-0033 D2), and the
assess worker's use of it at startup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from vektra_app import lms_providers
from vektra_app.lms_providers import GROUP, available_lms_providers, load_lms_provider
from vektra_shared.lms import GuardedLmsProvider, LmsProvider
from vektra_shared.registry import TEST_DOUBLE_MARKER
from vektra_shared.startup import StartupValidationError

#: instances built by the provider classes below, so a test can see what was closed
BUILT: list[Any] = []


def _provider_class(name: str, *, double: bool = False) -> type:
    """A class with every Protocol member: enough for the runtime check, nothing more."""

    def method(*_a: Any, **_k: Any) -> None:
        return None

    async def aclose(self: Any) -> None:
        self.closed = True

    def init(self: Any, *, development: bool = False) -> None:
        self.closed = False
        self.development = development
        BUILT.append(self)

    attrs: dict[str, Any] = {
        m: method
        for m in LmsProvider.__protocol_attrs__  # type: ignore[attr-defined]
        if m not in ("name", "setup_items", "required_secrets", "call_deadline_s")
    }
    attrs.update(
        name=name,
        setup_items=(),
        required_secrets=(),
        call_deadline_s=30.0,
        aclose=aclose,
        __init__=init,
    )
    if double:
        attrs[TEST_DOUBLE_MARKER] = True
    return type(f"Provider_{name}", (), attrs)


@dataclass
class _EP:
    name: str
    factory: Callable[[], Any]

    def load(self) -> Callable[[], Any]:
        return self.factory


def _eps(*eps: _EP) -> Callable[[], list[_EP]]:
    return lambda: list(eps)


async def _load(name: str | None, **kw: Any) -> Any:
    kw.setdefault("development", False)
    return await load_lms_provider(name, **kw)


@pytest.fixture(autouse=True)
def _clear_built() -> None:
    BUILT.clear()


def test_the_group_is_the_one_adr_0033_names() -> None:
    assert GROUP == "vektra.lms_providers"


def test_the_test_double_marker_is_the_registry_constant() -> None:
    """Review of #203: one definition of the marker, not a literal per module."""
    assert lms_providers.TEST_DOUBLE_MARKER is TEST_DOUBLE_MARKER
    assert not hasattr(lms_providers, "_TEST_DOUBLE_MARKER")


async def test_a_named_provider_is_built_and_checked() -> None:
    cls = _provider_class("alpha")
    provider = await _load("alpha", entry_points=_eps(_EP("alpha", cls)))
    assert isinstance(provider, cls) and isinstance(provider, LmsProvider)


async def test_an_unknown_name_is_refused_with_the_available_ones() -> None:
    eps = _eps(
        _EP("beta", _provider_class("beta")), _EP("alpha", _provider_class("alpha"))
    )
    with pytest.raises(StartupValidationError) as e:
        await _load("gamma", entry_points=eps)
    assert e.value.step == "lms_provider"
    assert "VEKTRA_ASSESS_LMS_PROVIDER=gamma" in e.value.detail
    assert "available providers: alpha, beta" in e.value.detail


@pytest.mark.parametrize("name", [None, ""])
async def test_an_unset_name_is_refused(name: str | None) -> None:
    with pytest.raises(
        StartupValidationError, match="is unset; available providers: none installed"
    ):
        await _load(name, entry_points=_eps())


async def test_two_distributions_with_one_name_are_refused() -> None:
    cls = _provider_class("alpha")
    with pytest.raises(StartupValidationError, match="declared by 2"):
        await _load("alpha", entry_points=_eps(_EP("alpha", cls), _EP("alpha", cls)))


async def test_a_factory_error_is_refused_without_its_text() -> None:
    def boom(**_: Any) -> Any:
        raise RuntimeError("password=hunter2")

    with pytest.raises(StartupValidationError) as e:
        await _load("alpha", entry_points=_eps(_EP("alpha", boom)))
    assert "RuntimeError" in e.value.detail and "hunter2" not in str(e.value.detail)
    assert e.value.__cause__ is None


async def test_something_that_is_not_a_provider_is_refused() -> None:
    with pytest.raises(StartupValidationError, match="does not implement LmsProvider"):
        await _load("alpha", entry_points=_eps(_EP("alpha", lambda **_: object())))


async def test_a_marked_test_double_is_refused_before_it_is_built() -> None:
    """Review of #203: building a test double can already have effects (a socket, a thread):
    the marker on the entry point's object is checked before the factory runs."""
    eps = _eps(_EP("fake", _provider_class("fake", double=True)))
    with pytest.raises(StartupValidationError, match="test double"):
        await _load("fake", entry_points=eps)
    assert BUILT == []
    assert await _load("fake", entry_points=eps, allow_test_doubles=True)


async def test_a_double_returned_by_a_plain_factory_is_refused_and_closed() -> None:
    cls = _provider_class("fake", double=True)
    with pytest.raises(StartupValidationError, match="test double"):
        await _load("fake", entry_points=_eps(_EP("fake", lambda **_: cls())))
    assert len(BUILT) == 1 and BUILT[0].closed


async def test_a_name_mismatch_is_refused_and_the_built_provider_closed() -> None:
    with pytest.raises(StartupValidationError, match="calls itself beta"):
        await _load("alpha", entry_points=_eps(_EP("alpha", _provider_class("beta"))))
    assert len(BUILT) == 1 and BUILT[0].closed


async def test_a_non_provider_with_an_aclose_is_closed_too() -> None:
    class Half:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    made: list[Half] = []

    def factory(**_: Any) -> Half:
        made.append(Half())
        return made[-1]

    with pytest.raises(StartupValidationError, match="does not implement"):
        await _load("alpha", entry_points=_eps(_EP("alpha", factory)))
    assert made[0].closed


def test_no_platform_distribution_declares_a_provider() -> None:
    """ADR-0033 D6: providers are separate distributions (``vektra-lms-*``, released from
    their own repositories). The real group is read, and whatever it lists comes from one of them,
    never from a platform package: in this workspace's own venv it lists nothing."""
    from importlib.metadata import entry_points

    eps = list(entry_points(group=GROUP))
    assert available_lms_providers() == tuple(sorted({ep.name for ep in eps}))
    for ep in eps:
        assert ep.dist is not None and ep.dist.name.startswith("vektra-lms-"), ep


async def test_an_aclose_property_that_raises_does_not_replace_the_refusal() -> None:
    """CodeRabbit 4543758772: the best-effort close must not let its own lookup escape."""

    class BadClose:
        @property
        def aclose(self) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(StartupValidationError, match="does not implement"):
        await _load("alpha", entry_points=_eps(_EP("alpha", lambda **_: BadClose())))


async def test_a_name_property_that_raises_is_refused_and_the_provider_closed() -> None:
    """CodeRabbit 4543758779: the runtime Protocol check does not call the property."""
    cls = _provider_class("alpha")

    def bad_name(self: Any) -> str:
        raise RuntimeError("secret=hunter2")

    broken = type("Broken", (cls,), {"name": property(bad_name)})
    with pytest.raises(StartupValidationError) as e:
        await _load("alpha", entry_points=_eps(_EP("alpha", broken)))
    assert "name" in e.value.detail and "RuntimeError" in e.value.detail
    assert "hunter2" not in e.value.detail and e.value.__cause__ is None
    assert len(BUILT) == 1 and BUILT[0].closed


def test_the_real_group_is_the_one_read() -> None:
    """CodeRabbit 4543186516: discovery reads ``vektra.lms_providers`` from the installed
    metadata; checked on an injected metadata function, so an installed provider does not
    make the suite fail."""
    seen: list[str] = []

    def fake_entry_points(*, group: str) -> list[_EP]:
        seen.append(group)
        return [_EP("beta", object), _EP("alpha", object)]

    import importlib.metadata as md

    real = md.entry_points
    md.entry_points = fake_entry_points  # type: ignore[assignment]
    try:
        assert available_lms_providers() == ("alpha", "beta")
    finally:
        md.entry_points = real
    assert seen == [GROUP]


def test_only_the_worker_composition_loads_a_provider() -> None:
    """The worker loads its provider at startup (2a-2); the API process never does."""
    import inspect

    import vektra_app.assess_worker as worker
    import vektra_app.main as main

    # the worker finds, checks the declared secrets, then builds (review of #207, finding 7)
    assert "find_lms_provider" in inspect.getsource(worker)
    assert "build_lms_provider" in inspect.getsource(worker)
    for name in ("find_lms_provider", "build_lms_provider", "load_lms_provider"):
        assert name not in inspect.getsource(main)
    assert lms_providers.SETTING == "VEKTRA_ASSESS_LMS_PROVIDER"


# --- the assess worker's startup step (assess_worker._load_lms) ------------------------------


def _run_load(worker: Any, *args: Any) -> Any:
    import asyncio

    return asyncio.run(worker._load_lms(*args))


class _Startup:
    """A provider with lifecycle only (every read member a stub): ``problems`` are its startup
    problems, ``seen`` the environments it was asked about."""

    name = "fake"
    setup_items: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    call_deadline_s = 30.0
    __vektra_test_double__ = True
    problems: tuple[Any, ...] = ()
    seen: list[bool] = []

    def __init__(self, *, development: bool = False) -> None:
        self.development = development

    def startup_problems(self, development: bool) -> tuple[Any, ...]:
        type(self).seen.append(development)
        return self.problems

    async def aclose(self) -> None:
        return None


for _m in LmsProvider.__protocol_attrs__:  # type: ignore[attr-defined]
    if not hasattr(_Startup, _m):
        setattr(_Startup, _m, lambda *_a, **_k: None)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Any:
    from vektra_assess.config import VektraAssessSettings

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return VektraAssessSettings()


def test_the_worker_refuses_to_start_without_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0033 D1: assess enabled and no provider named is a refusal at startup, naming the
    setting and the installed providers (the vertical is unavailable without one)."""
    from vektra_app import assess_worker

    settings = _settings(monkeypatch, VEKTRA_ASSESS_LMS_PROVIDER=None)
    with pytest.raises(StartupValidationError) as e:
        _run_load(assess_worker, settings)
    assert "VEKTRA_ASSESS_LMS_PROVIDER is unset" in e.value.detail
    listed = ", ".join(available_lms_providers()) or "none installed"
    assert f"available providers: {listed}" in e.value.detail


@pytest.mark.parametrize("env", ["production", None])
def test_the_fake_is_refused_outside_development(
    monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    """The chaos and e2e workers offer a test double through a development-only entry point:
    outside an explicit ``VEKTRA_ENV=development`` (unset included) it never loads."""
    from vektra_app import assess_worker

    settings = _settings(monkeypatch, VEKTRA_ASSESS_LMS_PROVIDER="fake", VEKTRA_ENV=env)
    with pytest.raises(StartupValidationError, match="test double"):
        _run_load(assess_worker, settings, _eps(_EP("fake", _Startup)))


def test_the_fake_loads_with_explicit_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app import assess_worker

    settings = _settings(
        monkeypatch, VEKTRA_ASSESS_LMS_PROVIDER="fake", VEKTRA_ENV="development"
    )
    _Startup.seen = []
    provider = _run_load(assess_worker, settings, _eps(_EP("fake", _Startup)))
    # behind the one fail-closed wrapper (review finding 8), the loaded provider inside it
    assert isinstance(provider, GuardedLmsProvider)
    assert isinstance(provider.inner, _Startup) and _Startup.seen == [True]


@pytest.mark.parametrize("env", [None, "production"])
def test_the_provider_is_never_told_development_unless_it_is_set(
    monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    """2a-1.11: development counts only when VEKTRA_ENV says so explicitly; the provider's own
    development-only switches are judged on the same rule."""
    from vektra_app import assess_worker

    class Real(_Startup):
        __vektra_test_double__ = False

    Real.seen = []
    settings = _settings(monkeypatch, VEKTRA_ASSESS_LMS_PROVIDER="fake", VEKTRA_ENV=env)
    _run_load(assess_worker, settings, _eps(_EP("fake", Real)))
    assert Real.seen == [False]


def test_a_provider_startup_problem_refuses_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app import assess_worker
    from vektra_shared.lms import StartupProblem

    class Problem(_Startup):
        __vektra_test_double__ = False
        problems = (StartupProblem("X_SETTING", "X_SETTING is wrong", "Unset it."),)

    settings = _settings(
        monkeypatch, VEKTRA_ASSESS_LMS_PROVIDER="fake", VEKTRA_ENV="production"
    )
    with pytest.raises(StartupValidationError) as e:
        _run_load(assess_worker, settings, _eps(_EP("fake", Problem)))
    assert e.value.detail == "X_SETTING is wrong" and e.value.remediation == "Unset it."


# --- review of #207: static secrets, the development flag, the provider's own explanation ---------


def _declaring(
    name: str, secrets: tuple[str, ...], built: tuple[str, ...] | None = None
) -> type:
    cls = _provider_class(name)
    cls.REQUIRED_SECRETS = secrets  # type: ignore[attr-defined]
    cls.required_secrets = secrets if built is None else built  # type: ignore[attr-defined]
    return cls


def test_the_declared_secrets_are_read_without_calling_the_factory() -> None:
    """Finding 1: the API learns what to refuse from the entry points alone."""
    from vektra_app.lms_providers import installed_provider_secrets

    eps = _eps(
        _EP("alpha", _declaring("alpha", ("assess_a_tokens",))),
        _EP("beta", _declaring("beta", ("assess_b_tokens", "assess_a_tokens"))),
        _EP("gamma", _provider_class("gamma")),  # declares nothing
    )
    assert installed_provider_secrets(eps) == ("assess_a_tokens", "assess_b_tokens")
    assert BUILT == []


@pytest.mark.parametrize("bad", [["assess_x"], "assess_x", (1,)])
def test_a_declaration_that_is_not_a_tuple_of_names_is_refused(bad: object) -> None:
    from vektra_app.lms_providers import installed_provider_secrets

    cls = _provider_class("alpha")
    cls.REQUIRED_SECRETS = bad  # type: ignore[attr-defined]
    with pytest.raises(StartupValidationError, match="REQUIRED_SECRETS"):
        installed_provider_secrets(_eps(_EP("alpha", cls)))


async def test_the_factory_receives_the_resolved_development_flag() -> None:
    """Finding 8: the provider is told, it never re-derives it."""
    cls = _provider_class("alpha")
    for development in (True, False):
        provider = await load_lms_provider(
            "alpha", development=development, entry_points=_eps(_EP("alpha", cls))
        )
        assert provider.development is development  # type: ignore[attr-defined]


async def test_a_provider_configuration_error_is_surfaced_as_the_provider_explains_it() -> (
    None
):
    """Finding 5: an invalid provider setting names the variable and its rule, not
    "failed to load (ValidationError)"."""
    from vektra_shared.lms import ProviderConfigurationError

    def factory(**_: Any) -> Any:
        raise ProviderConfigurationError(
            "X_RPS: Input should be greater than 0", "Fix X_RPS."
        )

    with pytest.raises(StartupValidationError) as e:
        await _load("alpha", entry_points=_eps(_EP("alpha", factory)))
    assert e.value.detail == "X_RPS: Input should be greater than 0"
    assert e.value.remediation == "Fix X_RPS."


async def test_a_built_declaration_other_than_the_static_one_is_refused_and_closed() -> (
    None
):
    """The key matrix checks the static REQUIRED_SECRETS before the build: a provider that then
    declares other secrets would be bound on names nobody checked."""
    cls = _declaring("alpha", ("assess_a_tokens",), built=("assess_other",))
    with pytest.raises(StartupValidationError, match="declares REQUIRED_SECRETS"):
        await _load("alpha", entry_points=_eps(_EP("alpha", cls)))
    assert len(BUILT) == 1 and BUILT[0].closed
