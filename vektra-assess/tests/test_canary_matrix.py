"""NFR-017 / BR-011 / NFR-015 evidence: the full canary matrix through all 8 sinks.

One test per Moodle version runs every case of that version and counts canaries per sink per case,
so the executed-case count can be compared with the generated count whatever the test order.
Each sink is asserted NON-EMPTY before it is searched: an unwired sink cannot pass vacuously.
"""

from __future__ import annotations

import io
import logging
from collections import Counter

import pytest
import structlog
from canary.matrix import (
    QTYPES,
    REVIEW_VARIANTS,
    VERSIONS,
    build_matrix,
    required_cells,
)
from canary.runner import SINKS, run_case

from vektra_shared.log_safety import configure_assess_worker_logging
from vektra_shared.testing_recorders import find_canaries

MATRIX = build_matrix()


def test_the_matrix_covers_every_cell() -> None:
    """Self-check on the GENERATED cases (forbidden sets are filled while they run)."""
    quiz = [c for c in MATRIX if c.kind == "quiz"]
    assert len(quiz) == len(VERSIONS) * 2 * 2 * len(REVIEW_VARIANTS)
    assert {c.review for c in quiz} == set(REVIEW_VARIANTS)
    assert len(MATRIX) == len(quiz) + len(VERSIONS) * 2 * 7


EXECUTED: Counter[str] = Counter()
COVERED: set[tuple[str, str]] = set()


@pytest.mark.parametrize("version", VERSIONS)
async def test_zero_forbidden_canaries_in_every_sink(version: str) -> None:
    cases = [c for c in MATRIX if c.version == version]
    failures: list[str] = []
    for case in cases:
        buf = io.StringIO()
        configure_assess_worker_logging(buf, level=logging.DEBUG)
        try:
            out = await run_case(case)
        finally:
            structlog.reset_defaults()
            for h in list(logging.getLogger("vektra_assess").handlers):
                logging.getLogger("vektra_assess").removeHandler(h)
        out.sinks["logs"] = buf.getvalue().encode()
        EXECUTED[version] += 1
        assert out.sinks["logs"], f"{case.name}: log sink empty"
        # 8: RAG OFF means zero vector-store calls, on every path
        if out.vector_calls:
            failures.append(f"{case.name}: {out.vector_calls} vector store call(s)")
        # the solution is never even fetched with the toggle OFF
        if not case.toggle and out.solution_fetches:
            failures.append(f"{case.name}: solution fetched with the toggle OFF")
        if case.shape == "unreleased":
            if out.not_eligible is None or out.built is not None:
                failures.append(
                    f"{case.name}: an unreleased grade must be NotEligible with no snapshot"
                )
            sinks = {"logs": out.sinks["logs"]}
        else:
            if out.built is None:
                failures.append(
                    f"{case.name}: expected a snapshot, got {out.not_eligible}"
                )
                continue
            sinks = out.sinks
            for name in SINKS:
                if name != "vector_store" and not sinks.get(name):
                    failures.append(f"{case.name}: sink {name} is empty")
        assert case.forbidden, f"{case.name}: no forbidden canary was planted"
        for name, blob in sinks.items():
            hits = {c: n for c, n in find_canaries(blob, case.forbidden).items() if n}
            if hits:
                failures.append(
                    f"{case.name}: {name}: {sorted(case.forbidden[c] for c in hits)}"
                )
        for cnry, where in case.required.items():
            for name in where:
                if name in sinks and find_canaries(sinks[name], [cnry])[cnry] == 0:
                    failures.append(
                        f"{case.name}: positive control {cnry[:30]} missing from {name}"
                    )
        COVERED.update(case.forbidden.values())
        # the Part A label follows the toggle
        if "part_a" in sinks:
            shown = b"Soluzione: mostrata" in sinks["part_a"]
            if shown is not case.toggle:
                failures.append(
                    f"{case.name}: Part A solution label {shown} with toggle {case.toggle}"
                )
    assert EXECUTED[version] == len(cases), "every generated case ran"
    assert failures == [], "\n".join(failures[:40])


def test_every_forbidden_cell_was_exercised() -> None:
    """Runs after the matrix tests in this module (pytest-randomly is not installed: see the
    executed-count assertion above for the order-free half of the check)."""
    if set(EXECUTED) != set(VERSIONS):
        pytest.skip("the matrix tests did not run in this session (filtered with -k)")
    missing = sorted(required_cells() - COVERED)
    assert missing == [], missing
    assert {t for f, t in COVERED if f == "right_answer"} == set(QTYPES)


async def test_the_matrix_detects_a_leak_when_pruning_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: break the sanitizer and the same case must show the planted canaries."""
    from canary.matrix import build_matrix as fresh

    from vektra_assess.parser_sidecar import html_review

    monkeypatch.setattr(html_review, "DROP_CLASSES", frozenset())
    case = next(
        c
        for c in fresh()
        if c.kind == "quiz" and c.review == "all_hidden" and not c.toggle
    )
    out = await run_case(case)
    assert out.built is not None
    leaked = {
        case.forbidden[c][0]
        for c, n in find_canaries(out.sinks["bundle"], case.forbidden).items()
        if n
    }
    assert "kept_block_feedback" in leaked


async def test_the_matrix_detects_a_widened_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: a capture that ignores hidden marks must be caught."""
    from canary.matrix import build_matrix as fresh

    from vektra_assess.context.capture import capture as cap
    from vektra_assess.context.policy import DisclosurePolicy

    real = cap.freeze

    def widened(inputs):  # type: ignore[no-untyped-def]
        fp = real(inputs)
        open_ = fp.policy.model_copy(update={"marks": True, "max_marks": True})
        return type(fp)(
            inputs=fp.inputs, policy=DisclosurePolicy.model_validate(open_.model_dump())
        )

    monkeypatch.setattr(cap, "freeze", widened)
    case = next(
        c
        for c in fresh()
        if c.kind == "quiz" and c.review == "all_hidden" and not c.toggle
    )
    out = await run_case(case)
    assert out.built is not None
    leaked = {
        case.forbidden[c][0]
        for c, n in find_canaries(out.sinks["bundle"], case.forbidden).items()
        if n
    }
    assert {"hidden_mark", "correctness"} <= leaked


@pytest.mark.parametrize("review", list(REVIEW_VARIANTS))
async def test_exclusion_event_hash_is_over_the_expected_path_set(review: str) -> None:
    """The expected set is derived HERE from the review options, independently of capture."""
    import hashlib

    from canary.matrix import build_matrix as fresh

    case = next(
        c
        for c in fresh()
        if c.kind == "quiz"
        and c.review == review
        and not c.toggle
        and c.version == "5.2"
    )
    out = await run_case(case)
    assert out.built is not None
    fb, gfb, ra, mk = REVIEW_VARIANTS[review]
    expected = {"solution_text", "overall_feedback"}
    for item in out.built.snapshot.quiz_items:
        p = f"q{item.label}"
        expected.add(f"{p}.review_html")
        if not ra:
            expected.add(f"{p}.right_answer")
        if not fb:
            expected.add(f"{p}.specific_feedback")
        if not gfb:
            expected.add(f"{p}.general_feedback")
        if mk != 2:
            expected |= {f"{p}.mark", f"{p}.correctness"}
        if mk == 0:
            expected.add(f"{p}.max_mark")
    digest = hashlib.sha256("\n".join(sorted(expected)).encode()).hexdigest()
    (event,) = [
        e for e in out.built.audit if e.event == "assess_context_exclusion_applied"
    ]
    assert event.data["excluded_field_set_hash"] == digest
    assert find_canaries(str(event.data), case.forbidden) == dict.fromkeys(
        case.forbidden, 0
    )
