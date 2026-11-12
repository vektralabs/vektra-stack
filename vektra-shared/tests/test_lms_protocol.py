"""The LmsProvider Protocol and its neutral types (ADR-0033 D2, plan 2a-1.1 to 2a-1.3)."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from vektra_shared import lms
from vektra_shared.lms import (
    AttemptReview,
    AttemptState,
    HtmlExtractionProfile,
    LmsProvider,
    QuestionKind,
    QuestionState,
    Readable,
    ReviewHtml,
    ReviewItem,
    ShapeEvidence,
    Unreadable,
    UnreadableReason,
)

#: ADR-0033 D2, the 23 members plus `required_secrets` (PR 2a-2 review): name them here so a
#: silent change of the Protocol is red
PROTOCOL_MEMBERS = frozenset(
    {
        "name",
        "setup_items",
        "required_secrets",
        "startup_problems",
        "aclose",
        "site_info",
        "service_account_check",
        "probe_capabilities",
        "resolve_activity",
        "resolve_in_course",
        "enrolment",
        "course_staff",
        "quiz_settings",
        "review_options",
        "attempts",
        "attempt_review",
        "assignment_settings",
        "assignment_dates",
        "submission",
        "submission_file",
        "assignment_release",
        "grade_item",
        "grading_definition",
        "rubric_fillings",
    }
)


def test_the_protocol_has_the_24_members() -> None:
    assert len(PROTOCOL_MEMBERS) == 24
    assert set(LmsProvider.__protocol_attrs__) == PROTOCOL_MEMBERS  # type: ignore[attr-defined]


def test_the_protocol_is_re_exported_as_the_11th() -> None:
    from vektra_shared import protocols

    assert protocols.LmsProvider is LmsProvider
    assert "11 Protocol interfaces" in (protocols.__doc__ or "")


def test_startup_problems_takes_the_resolved_development_flag_not_the_raw_env() -> None:
    """A raw ``VEKTRA_ENV`` string would let a provider count the platform default
    ``development`` as development: the composition root passes the resolved flag."""
    import inspect

    params = inspect.signature(LmsProvider.startup_problems).parameters
    assert list(params) == ["self", "development"]
    assert params["development"].annotation in (bool, "bool")


def test_the_protocol_is_runtime_checkable() -> None:
    class Incomplete:
        name = "x"

    assert not isinstance(Incomplete(), LmsProvider)


def test_shape_evidence_names_the_call_not_a_wire_function() -> None:
    fields = [f.name for f in dataclasses.fields(ShapeEvidence)]
    assert fields == ["call", "release", "signature"]


@pytest.mark.parametrize("code", ["has space", "x" * 65, "a;b"])
def test_detail_code_cannot_carry_free_text(code: str) -> None:
    with pytest.raises(ValueError):
        Unreadable(UnreadableReason.TIMEOUT, code)


def test_unknown_question_state_is_its_own_value() -> None:
    """``UNKNOWN`` exists so a provider never has to guess a final state (fail closed)."""
    finals = {QuestionState.FINAL_MARKED, QuestionState.FINAL_NO_MARK}
    assert QuestionState.UNKNOWN not in finals and QuestionState.PENDING not in finals


def test_without_html_drops_every_review_html() -> None:
    profile = HtmlExtractionProfile(
        keep={"question_text": "q"}, drop_classes=frozenset({"d"}), profile_version="1"
    )
    item = ReviewItem(
        slot=1,
        display_number="1",
        question_kind=QuestionKind.ESSAY,
        state=QuestionState.FINAL_MARKED,
        status_label=None,
        mark=1.0,
        max_mark=2.0,
        needs_grading=False,
        html=ReviewHtml("<div class='q'>x</div>", profile),
    )
    review = AttemptReview(1, 2, 3, AttemptState.FINISHED, 1.0, 5.0, (item, item))
    assert all(i.html is None for i in review.without_html().items)
    assert review.items[0].html is not None  # frozen: the original is untouched


_FORBIDDEN_FIELD_WORDS = ("solution", "rightanswer", "right_answer", "hidden_feedback")


def test_no_neutral_type_has_a_slot_for_forbidden_content() -> None:
    """ADR-0032 layer 1 at the type level. ``ReviewVisibility.show_right_answer`` is a flag,
    not content, and is the one allowed name with such a word in it."""
    bad = []
    for name in lms.__all__:
        obj = getattr(lms, name)
        if not dataclasses.is_dataclass(obj):
            continue
        for f in dataclasses.fields(obj):
            if f.name.startswith("show_"):
                continue
            if any(w in f.name for w in _FORBIDDEN_FIELD_WORDS):
                bad.append(f"{name}.{f.name}")
    assert bad == []


def test_importing_the_lms_package_pulls_no_component_and_no_http_client() -> None:
    """The import-linter contract proves it statically; this proves it at runtime."""
    code = (
        "import sys, vektra_shared.lms; "
        "print(sorted({m.split('.')[0] for m in sys.modules "
        "if m.split('.')[0] in ('httpx', 'vektra_assess', 'vektra_app', 'vektra_core', "
        "'vektra_learn', 'sqlalchemy')}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_readable_carries_its_evidence() -> None:
    ev = ShapeEvidence("get_x", "1.0", "0" * 64)
    assert Readable(3, ev).evidence is ev
