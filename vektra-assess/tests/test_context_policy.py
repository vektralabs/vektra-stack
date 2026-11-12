"""DisclosurePolicy over the full flag lattice (BR-011). Every unknown fails closed."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from vektra_assess.context.policy import (
    POLICY_VERSION,
    HtmlPath,
    PolicyInputs,
    ReviewOptions,
    derive_policy,
    freeze,
)
from vektra_assess.context.snapshot import FrozenFlags

STATES = ("readable", "partial", "unreadable")
REVIEWS: list[ReviewOptions | None] = [None] + [
    ReviewOptions(
        feedback=f, generalfeedback=g, rightanswer=r, overallfeedback=o, marks=m
    )
    for f, g, r, o, m in itertools.product(
        [False, True], [False, True], [False, True], [False, True], [0, 1, 2]
    )
]


def _inputs(**kw: Any) -> PolicyInputs:
    base: dict[str, Any] = {
        "flags": FrozenFlags(show_solution=False, rubric_source="none"),
        "review": None,
        "review_html_filterable": "unreadable",
        "rubric_display_settings": "unreadable",
        "rubric_descriptors_visible": None,
        "released": True,
    }
    return PolicyInputs(**(base | kw))


def _lattice() -> list[PolicyInputs]:
    out = []
    for sol, src, review, html, rub, vis, rel in itertools.product(
        [False, True],
        ["none", "pasted", "lms"],
        REVIEWS,
        STATES,
        STATES,
        [None, False, True],
        [False, True],
    ):
        out.append(
            _inputs(
                flags=FrozenFlags(show_solution=sol, rubric_source=src),
                review=review,
                review_html_filterable=html,
                rubric_display_settings=rub,
                rubric_descriptors_visible=vis,
                released=rel,
            )
        )
    return out


LATTICE = _lattice()


def test_lattice_is_complete() -> None:
    assert len(LATTICE) == 2 * 3 * len(REVIEWS) * 3 * 3 * 3 * 2 == 15_876


def test_every_cell_follows_the_decision_table() -> None:
    for inp in LATTICE:
        p = derive_policy(inp)
        r = inp.review
        assert p.version == POLICY_VERSION
        assert p.eligible is inp.released
        assert p.solution is inp.flags.show_solution
        assert p.pasted_rubric is (inp.flags.rubric_source == "pasted")
        if r is None:  # unreadable review options hide everything they govern
            assert not any(
                [
                    p.marks,
                    p.max_marks,
                    p.specific_feedback,
                    p.general_feedback,
                    p.right_answer,
                    p.overall_feedback,
                ]
            )
        else:
            assert (p.marks, p.max_marks) == (r.marks == 2, r.marks >= 1)
            assert (
                p.specific_feedback,
                p.general_feedback,
                p.right_answer,
                p.overall_feedback,
            ) == (r.feedback, r.generalfeedback, r.rightanswer, r.overallfeedback)
        # only "readable" selects the filterable path; partial and unreadable fall back
        assert (p.html_path is HtmlPath.FILTERABLE) is (
            inp.review_html_filterable == "readable"
        )
        # descriptors need the setting readable AND explicitly visible
        assert p.rubric_descriptors is (
            inp.rubric_display_settings == "readable"
            and inp.rubric_descriptors_visible is True
        )


def test_removal_classes_follow_the_exclusions() -> None:
    hidden = derive_policy(_inputs())
    assert hidden.removal_classes() == (
        "generalfeedback",
        "grade",
        "outcome",
        "rightanswer",
        "specificfeedback",
    )
    open_ = derive_policy(
        _inputs(
            review=ReviewOptions(
                feedback=True,
                generalfeedback=True,
                rightanswer=True,
                overallfeedback=True,
                marks=2,
            )
        )
    )
    assert open_.removal_classes() == ()


def test_oq025_defaults_are_the_fallbacks() -> None:
    """Spike result: HTML not filterable and rubric display unreadable on 4.3 and 5.2."""
    p = derive_policy(_inputs())
    assert p.html_path is HtmlPath.STRUCTURED_ONLY and p.rubric_descriptors is False


def test_frozen_policy_round_trips_and_is_immutable() -> None:
    fp = freeze(_inputs(flags=FrozenFlags(show_solution=True, rubric_source="pasted")))
    again = type(fp).model_validate_json(fp.model_dump_json())
    assert again == fp
    with pytest.raises(Exception, match="frozen"):
        fp.policy.solution = False  # type: ignore[misc]
    with pytest.raises(Exception, match="extra"):
        PolicyInputs.model_validate(_inputs().model_dump() | {"unknown": 1})
