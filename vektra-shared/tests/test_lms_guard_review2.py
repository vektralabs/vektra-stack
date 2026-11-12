"""Second review of #205 (findings 2, 3 and 10): the profile is frozen whatever container it got,
an unreadable provider name has a declared failure, and the wrapper exposes the Protocol only."""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest

from vektra_shared.lms import (
    GuardedLmsProvider,
    HtmlExtractionProfile,
    LmsProvider,
    QuestionKind,
    guarded,
)
from vektra_shared.lms import guard as guard_module


class NameAndStartupRaise:
    @property
    def name(self) -> str:
        raise RuntimeError("secret=hunter2")

    def startup_problems(self, development: bool) -> tuple[Any, ...]:
        raise RuntimeError("secret=hunter2")


for _m in LmsProvider.__protocol_attrs__:  # type: ignore[attr-defined]
    if not hasattr(NameAndStartupRaise, _m):
        setattr(NameAndStartupRaise, _m, lambda *_a, **_k: None)


def test_a_profile_given_a_set_is_frozen_and_hashable() -> None:
    drop = {"b", "a"}
    keep = {"question_text": "qt"}
    p = HtmlExtractionProfile(
        keep=keep,
        drop_classes=drop,  # type: ignore[arg-type]
        profile_version="v1",
        kinds=[QuestionKind.ESSAY],  # type: ignore[arg-type]
    )
    assert isinstance(p.drop_classes, frozenset) and isinstance(p.kinds, frozenset)
    hash(p.drop_classes)
    drop.add("c")
    keep["response"] = "r"
    assert p.drop_classes == {"a", "b"} and dict(p.keep) == {"question_text": "qt"}
    with pytest.raises(TypeError):
        p.keep["x"] = "y"  # type: ignore[index]


def test_a_provider_whose_name_and_startup_check_raise_still_refuses_by_setting() -> (
    None
):
    g = guarded(NameAndStartupRaise())  # type: ignore[arg-type]
    assert g.name == guard_module.UNNAMED
    (problem,) = g.startup_problems(False)
    assert problem.setting == "VEKTRA_ASSESS_LMS_PROVIDER"
    assert "VEKTRA_ASSESS_LMS_PROVIDER" in problem.detail
    assert guard_module.UNNAMED in problem.detail and "hunter2" not in problem.detail


def test_only_the_protocol_is_reachable_through_the_wrapper() -> None:
    class WithExtra(NameAndStartupRaise):
        world = "a test handle"

    g = guarded(WithExtra())  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        _ = g.world  # type: ignore[attr-defined]
    assert isinstance(g, GuardedLmsProvider) and g.inner.world == "a test handle"


def test_a_copy_or_a_pickle_of_the_wrapper_does_not_recurse() -> None:
    g = GuardedLmsProvider.__new__(
        GuardedLmsProvider
    )  # ``inner`` unset, as copy/pickle do
    with pytest.raises(AttributeError):
        _ = g.inner
    copied = copy.copy(guarded(NameAndStartupRaise()))  # type: ignore[arg-type]
    assert isinstance(copied, GuardedLmsProvider)
    restored = pickle.loads(pickle.dumps(GuardedLmsProvider(_Picklable())))  # type: ignore[arg-type]
    assert isinstance(restored.inner, _Picklable)


class _Picklable:
    name = "p"
