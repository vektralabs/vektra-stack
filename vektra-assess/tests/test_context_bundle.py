"""ContextBundle: built only by assemble_context, never widened (ARCH-069, ADR-0032)."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
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


ESCAPES: dict[str, Callable[[ContextBundle], Any]] = {
    "init": lambda b: ContextBundle(**b.model_dump()),
    "init_forged_sentinel": lambda b: ContextBundle(
        _sentinel=object(), **b.model_dump()
    ),
    "model_construct": lambda b: ContextBundle.model_construct(**b.model_dump()),
    "model_validate": lambda b: ContextBundle.model_validate(b.model_dump()),
    "model_validate_json": lambda b: ContextBundle.model_validate_json(
        b.model_dump_json()
    ),
    "model_validate_strings": lambda b: ContextBundle.model_validate_strings({}),
    "model_copy": lambda b: b.model_copy(update={"solution_text": "x"}),
    "copy": copy.copy,
    "deepcopy": copy.deepcopy,
    "pickle": pickle.dumps,
}


@pytest.mark.parametrize("name", list(ESCAPES))
def test_every_escape_hatch_is_closed(name: str) -> None:
    with pytest.raises(BundleConstructionError):
        ESCAPES[name](_bundle())


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
