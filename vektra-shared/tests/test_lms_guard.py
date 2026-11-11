"""``vektra_shared.lms.guard``: the one fail-closed wrapper around an LMS provider (PR 2a-2
review, finding 8). Every Protocol member has a declared failure, and a member added to the
Protocol without one is caught, not left unguarded."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from vektra_shared.lms import (
    PROVIDER_ERROR,
    SETUP_UNVERIFIABLE,
    GuardedLmsProvider,
    LmsProvider,
    LmsSite,
    Readable,
    Unreadable,
    UnreadableReason,
    guarded,
)
from vektra_shared.lms import guard as guard_module

SITE = LmsSite("site-1", "https://lms.example.org", "1.0")


class Boom(RuntimeError):
    pass


class Raising:
    """Every Protocol member raises (the text carries a credential: it must never be logged)."""

    def __getattr__(self, name: str) -> Any:
        raise Boom("token=sekret-credential")

    @property
    def name(self) -> str:
        return "raising"


def _members() -> frozenset[str]:
    return frozenset(LmsProvider.__protocol_attrs__)  # type: ignore[attr-defined]


def test_every_protocol_member_has_a_declared_failure() -> None:
    assert guard_module.uncovered_members() == frozenset()
    assert (
        set(guard_module.READ_MEMBERS) | set(guard_module.OTHER_MEMBERS) == _members()
    )
    for member in _members():
        assert member in GuardedLmsProvider.__dict__, member


def test_a_member_added_to_the_protocol_is_reported_uncovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attrs = set(LmsProvider.__protocol_attrs__)  # type: ignore[attr-defined]
    monkeypatch.setattr(LmsProvider, "__protocol_attrs__", {*attrs, "new_member"})
    assert guard_module.uncovered_members() == {"new_member"}


async def test_every_read_member_fails_closed() -> None:
    p = guarded(Raising())  # type: ignore[arg-type]
    for member in guard_module.READ_MEMBERS:
        sig = inspect.signature(getattr(LmsProvider, member))
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        for name, param in list(sig.parameters.items())[1:]:
            value: Any = SITE if name == "site" else 1
            if name == "kind":
                value = "quiz"
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                kwargs[name] = value
            else:
                args.append(value)
        got = await getattr(p, member)(*args, **kwargs)
        assert got == Unreadable(UnreadableReason.UNKNOWN_SHAPE, PROVIDER_ERROR), member


async def test_the_other_members_have_their_declared_failure() -> None:
    p = guarded(Raising())  # type: ignore[arg-type]
    assert p.name == "raising"
    assert p.setup_items == (guard_module.INVALID_DECLARATION,)
    assert p.required_secrets == (guard_module.INVALID_DECLARATION,)
    (problem,) = p.startup_problems(False)
    assert "Boom" in problem.detail and "sekret" not in problem.detail
    await p.aclose()  # logged and dropped
    items = await p.service_account_check(SITE, Readable(None), None)  # type: ignore[arg-type]
    assert [(i.name, i.result, i.reason_code) for i in items] == [
        (guard_module.INVALID_DECLARATION, SETUP_UNVERIFIABLE, "check_error")
    ]
    flags = await p.probe_capabilities(SITE, None, ("a", "b"))
    assert [(f.flag, f.state, f.reason_code) for f in flags] == [
        ("a", "unreadable", "not_probed"),
        ("b", "unreadable", "not_probed"),
    ]


def test_guarding_is_idempotent_and_keeps_the_inner_handle() -> None:
    inner = Raising()
    once = guarded(inner)  # type: ignore[arg-type]
    assert guarded(once) is once
    assert isinstance(once, GuardedLmsProvider) and once.inner is inner
    assert isinstance(once, LmsProvider)
