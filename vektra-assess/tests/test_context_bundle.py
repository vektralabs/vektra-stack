"""ContextBundle: built only by assemble_context, never widened (ARCH-069, ADR-0032)."""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import BundleConstructionError, ContextBundle
from vektra_assess.context.policy import (
    FrozenPolicy,
    PolicyInputs,
    ReviewOptions,
    freeze,
)
from vektra_assess.context.snapshot import (
    AssignCriterion,
    FrozenFlags,
    MarksHidden,
    Snapshot,
    content_hash,
)


def _frozen(s: Snapshot, **flags: Any) -> FrozenPolicy:
    f = s.header.flags.model_copy(update=flags) if flags else s.header.flags
    return freeze(
        PolicyInputs(
            flags=f,
            review=None,
            review_html_filterable="unreadable",
            rubric_display_settings="unreadable",
            rubric_descriptors_visible=None,
            released=True,
        )
    )


def _bundle() -> ContextBundle:
    s = make()
    return assemble_context(s, _frozen(s))


def test_every_escape_hatch_is_closed() -> None:
    # each attempt sits inside pytest.raises(BundleConstructionError): the AST guard
    # (test_context_guards.py) treats only that as a legitimate mention outside the package
    b = _bundle()
    data = b.model_dump()
    with pytest.raises(BundleConstructionError):
        ContextBundle(**data)
    with pytest.raises(BundleConstructionError):
        ContextBundle(_sentinel=object(), **data)
    with pytest.raises(BundleConstructionError):
        ContextBundle.model_construct(**data)
    with pytest.raises(BundleConstructionError):
        ContextBundle.model_validate(data)
    with pytest.raises(BundleConstructionError):
        ContextBundle.model_validate_json(b.model_dump_json())
    with pytest.raises(BundleConstructionError):
        ContextBundle.model_validate_strings({})
    with pytest.raises(BundleConstructionError):
        b.model_copy(update={"solution_text": "x"})
    with pytest.raises(BundleConstructionError):
        copy.copy(b)
    with pytest.raises(BundleConstructionError):
        copy.deepcopy(b)
    with pytest.raises(BundleConstructionError):
        pickle.dumps(b)


def test_assembly_is_deterministic_and_bound_to_the_snapshot() -> None:
    s = make()
    a, b = assemble_context(s, _frozen(s)), assemble_context(s, _frozen(s))
    assert a == b and a.content_hash == content_hash(s)
    assert a.model_dump_json() == b.model_dump_json()


def test_a_toggle_switched_on_after_start_cannot_widen_the_bundle() -> None:
    s = make()  # captured with the toggle OFF: no solution in the snapshot
    with pytest.raises(ValueError, match="does not belong"):
        assemble_context(s, _frozen(s, show_solution=True))
    # even a snapshot that somehow carries a solution yields none under an OFF policy
    leaky = s.model_copy(update={"solution_text": "CNRY-solution"})
    assert assemble_context(leaky, _frozen(leaky)).solution_text is None


def _with_review(s: Snapshot, review: ReviewOptions, html: str) -> FrozenPolicy:
    return freeze(
        PolicyInputs(
            flags=s.header.flags,
            review=review,
            review_html_filterable=html,  # type: ignore[arg-type]
            rubric_display_settings="readable",
            rubric_descriptors_visible=True,
            released=True,
        )
    )


_ALL = ReviewOptions(
    feedback=True, generalfeedback=True, rightanswer=True, overallfeedback=True, marks=2
)
_NONE = ReviewOptions(
    feedback=False,
    generalfeedback=False,
    rightanswer=False,
    overallfeedback=False,
    marks=0,
)


def _rich() -> Snapshot:
    s = make(
        criteria=(
            AssignCriterion(
                label="C1",
                level_index=2,
                level_count=3,
                points="2",
                max_points="3",
                remark="ok",
                description="CNRY-desc",
                chosen_level_text="CNRY-level",
                adjacent_level_texts=("CNRY-adj",),
            ),
        ),
    )
    return s.model_copy(
        update={
            "overall": s.overall.model_copy(update={"overall_feedback": "CNRY-ofb"}),
            "exclusion": s.exclusion.model_copy(update={"html_structured_only": False}),
        }
    )


def test_a_permissive_policy_keeps_what_the_snapshot_holds() -> None:
    s = _rich()
    b = assemble_context(s, _with_review(s, _ALL, "readable"))
    assert [(i.mark, i.max_mark, i.status) for i in b.quiz_items] == [
        ("1", "1", "Corretta"),
        ("2.5", "5", None),
    ]
    assert b.quiz_items[1].review_text == "risposta"
    assert b.criteria[0].chosen_level_text == "CNRY-level"
    assert b.overall.overall_feedback == "CNRY-ofb"


def test_a_narrower_policy_removes_marks_feedback_and_descriptors() -> None:
    """Capture filters by the policy, and assembly re-applies it: a narrower policy paired with
    the same snapshot (same flags) cannot carry hidden content into the bundle."""
    s = _rich()
    frozen = _with_review(s, _NONE, "unreadable").model_copy(
        update={
            "policy": _with_review(s, _NONE, "unreadable").policy.model_copy(
                update={"rubric_descriptors": False}
            )
        }
    )
    b = assemble_context(s, frozen)
    for i in b.quiz_items:
        assert (i.mark, i.max_mark, i.state, i.status, i.review_text) == (None,) * 5
        assert i.marks_hidden is MarksHidden.REVIEW_OPTIONS
    c = b.criteria[0]
    assert (c.description, c.chosen_level_text, c.adjacent_level_texts) == (
        None,
        None,
        (),
    )
    assert b.overall.overall_feedback is None
    assert "CNRY" not in b.model_dump_json()


def test_review_text_is_dropped_when_the_snapshot_did_not_exclude_what_the_policy_hides() -> (
    None
):
    s = _rich()  # q2 has review text, and its exclusion record lists no q2 path
    only_marks = ReviewOptions(
        feedback=True,
        generalfeedback=True,
        rightanswer=False,
        overallfeedback=True,
        marks=2,
    )
    b = assemble_context(s, _with_review(s, only_marks, "readable"))
    assert (
        b.quiz_items[1].review_text is None
    )  # q2.right_answer was not excluded at capture
    ok = s.model_copy(
        update={
            "exclusion": s.exclusion.model_copy(
                update={
                    "excluded_paths": (
                        "q1.right_answer",
                        "q2.right_answer",
                        "solution_text",
                    )
                }
            )
        }
    )
    assert (
        assemble_context(ok, _with_review(ok, only_marks, "readable"))
        .quiz_items[1]
        .review_text
        == "risposta"
    )


def test_on_with_solution_and_pasted_rubric() -> None:
    s = make(solution_text="CNRY-sol", pasted_rubric="CNRY-rub")
    s = s.model_copy(
        update={
            "header": s.header.model_copy(
                update={
                    "flags": FrozenFlags(show_solution=True, rubric_source="pasted")
                }
            )
        }
    )
    b = assemble_context(s, _frozen(s))
    assert (b.solution_text, b.pasted_rubric, b.solution_shown) == (
        "CNRY-sol",
        "CNRY-rub",
        True,
    )


def test_bundle_fields_are_a_subset_of_the_snapshot_and_have_no_retrieval() -> None:
    fields = set(ContextBundle.model_fields)
    assert not {f for f in fields if "retriev" in f or "rag" in f or "chunk" in f}
    assert not fields & {"html", "attemptid", "cmid", "userid", "slot", "exclusion"}
