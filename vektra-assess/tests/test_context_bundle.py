"""ContextBundle: built only by assemble_context, never widened (ARCH-069, ADR-0032)."""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import BundleConstructionError, ContextBundle
from vektra_assess.context.policy import FrozenPolicy, PolicyInputs, freeze
from vektra_assess.context.snapshot import FrozenFlags, Snapshot, content_hash


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
