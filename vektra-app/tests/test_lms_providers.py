"""Entry-point discovery of the LMS provider (plan task 2a-1.5, ADR-0033 D2). Not wired yet."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from vektra_app import lms_providers
from vektra_app.lms_providers import GROUP, available_lms_providers, load_lms_provider
from vektra_shared.lms import LmsProvider
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

    def init(self: Any) -> None:
        self.closed = False
        BUILT.append(self)

    attrs: dict[str, Any] = {
        m: method
        for m in LmsProvider.__protocol_attrs__  # type: ignore[attr-defined]
        if m not in ("name", "setup_items")
    }
    attrs.update(name=name, setup_items=(), aclose=aclose, __init__=init)
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
    provider = await load_lms_provider("alpha", entry_points=_eps(_EP("alpha", cls)))
    assert isinstance(provider, cls) and isinstance(provider, LmsProvider)


async def test_an_unknown_name_is_refused_with_the_available_ones() -> None:
    eps = _eps(
        _EP("beta", _provider_class("beta")), _EP("alpha", _provider_class("alpha"))
    )
    with pytest.raises(StartupValidationError) as e:
        await load_lms_provider("gamma", entry_points=eps)
    assert e.value.step == "lms_provider"
    assert "VEKTRA_ASSESS_LMS_PROVIDER=gamma" in e.value.detail
    assert "available providers: alpha, beta" in e.value.detail


@pytest.mark.parametrize("name", [None, ""])
async def test_an_unset_name_is_refused(name: str | None) -> None:
    with pytest.raises(
        StartupValidationError, match="is unset; available providers: none installed"
    ):
        await load_lms_provider(name, entry_points=_eps())


async def test_two_distributions_with_one_name_are_refused() -> None:
    cls = _provider_class("alpha")
    with pytest.raises(StartupValidationError, match="declared by 2"):
        await load_lms_provider(
            "alpha", entry_points=_eps(_EP("alpha", cls), _EP("alpha", cls))
        )


async def test_a_factory_error_is_refused_without_its_text() -> None:
    def boom() -> Any:
        raise RuntimeError("password=hunter2")

    with pytest.raises(StartupValidationError) as e:
        await load_lms_provider("alpha", entry_points=_eps(_EP("alpha", boom)))
    assert "RuntimeError" in e.value.detail and "hunter2" not in str(e.value.detail)
    assert e.value.__cause__ is None


async def test_something_that_is_not_a_provider_is_refused() -> None:
    with pytest.raises(StartupValidationError, match="does not implement LmsProvider"):
        await load_lms_provider("alpha", entry_points=_eps(_EP("alpha", object)))


async def test_a_marked_test_double_is_refused_before_it_is_built() -> None:
    """Review of #203: building a test double can already have effects (a socket, a thread):
    the marker on the entry point's object is checked before the factory runs."""
    eps = _eps(_EP("fake", _provider_class("fake", double=True)))
    with pytest.raises(StartupValidationError, match="test double"):
        await load_lms_provider("fake", entry_points=eps)
    assert BUILT == []
    assert await load_lms_provider("fake", entry_points=eps, allow_test_doubles=True)


async def test_a_double_returned_by_a_plain_factory_is_refused_and_closed() -> None:
    cls = _provider_class("fake", double=True)
    with pytest.raises(StartupValidationError, match="test double"):
        await load_lms_provider("fake", entry_points=_eps(_EP("fake", lambda: cls())))
    assert len(BUILT) == 1 and BUILT[0].closed


async def test_a_name_mismatch_is_refused_and_the_built_provider_closed() -> None:
    with pytest.raises(StartupValidationError, match="calls itself beta"):
        await load_lms_provider(
            "alpha", entry_points=_eps(_EP("alpha", _provider_class("beta")))
        )
    assert len(BUILT) == 1 and BUILT[0].closed


async def test_a_non_provider_with_an_aclose_is_closed_too() -> None:
    class Half:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    made: list[Half] = []

    def factory() -> Half:
        made.append(Half())
        return made[-1]

    with pytest.raises(StartupValidationError, match="does not implement"):
        await load_lms_provider("alpha", entry_points=_eps(_EP("alpha", factory)))
    assert made[0].closed


def test_the_real_group_is_read_and_has_no_provider_yet() -> None:
    """Nothing in this repository declares a provider until plan task 2a-4b.1."""
    assert available_lms_providers() == ()


def test_not_wired_yet() -> None:
    """2a-1 adds the loader only: no composition root calls it before 2a-4b.5."""
    import inspect

    import vektra_app.assess_worker as worker
    import vektra_app.main as main

    for mod in (worker, main):
        assert "load_lms_provider" not in inspect.getsource(mod)
    assert lms_providers.SETTING == "VEKTRA_ASSESS_LMS_PROVIDER"
