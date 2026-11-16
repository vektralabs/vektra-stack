"""Course-material eligibility (FEAT-028, ARCH-072 amendment 2026-11-16).

One case per kind of test module of the 2026-11-16 spike (R0..R8): only R1, a plain visible
module with no condition in a visible and available section, is eligible.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.materials import (
    DEFAULT_OPT_OUT_TAG,
    EligibleModule,
    eligible,
    eligible_modules,
)
from vektra_shared.lms import CourseModuleVisibility

TAG = DEFAULT_OPT_OUT_TAG


def _m(
    cmid: int = 1,
    title: str = "Dispensa",
    *,
    visible: bool = True,
    on_page: bool = True,
    conditions: bool = False,
    section_visible: bool = True,
    section_available: bool = True,
) -> CourseModuleVisibility:
    return CourseModuleVisibility(
        cmid=cmid,
        title=title,
        section_id=10,
        visible=visible,
        on_course_page=on_page,
        has_conditions=conditions,
        section_visible=section_visible,
        section_available=section_available,
    )


#: R0..R8 of the spike: (kind, module, eligible)
CASES = [
    ("R1 plain visible module", _m(), True),
    ("R2 hidden module", _m(visible=False), False),
    ("R3 stealth module (not on the course page)", _m(on_page=False), False),
    ("R4 group restriction", _m(conditions=True), False),
    ("R5 date restriction", _m(conditions=True), False),
    ("R6 completion restriction", _m(conditions=True), False),
    ("R7 opt-out tag in the title", _m(title="Soluzioni [no-ai]"), False),
    ("R8 section restricted by date", _m(section_available=False), False),
    ("R0 hidden section", _m(section_visible=False), False),
]


@pytest.mark.parametrize(
    ("kind", "module", "expected"), CASES, ids=[c[0] for c in CASES]
)
def test_each_spike_kind(
    kind: str, module: CourseModuleVisibility, expected: bool
) -> None:
    assert eligible(module, TAG) is expected, kind


@pytest.mark.parametrize(
    "title", ["[NO-AI] soluzioni", "Esercizi [No-Ai]", "x[no-ai]y", "[no-ai]"]
)
def test_the_tag_matches_anywhere_case_insensitively(title: str) -> None:
    assert eligible(_m(title=title), TAG) is False


def test_a_title_without_the_exact_tag_is_eligible() -> None:
    assert eligible(_m(title="no ai, ma [noai]"), TAG) is True


def test_a_custom_tag() -> None:
    assert eligible(_m(title="Esame [riservato]"), "[riservato]") is False
    assert eligible(_m(title="Esame [no-ai]"), "[riservato]") is True


def test_an_empty_tag_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        eligible(_m(), "")


def test_eligible_modules_keeps_only_eligible_ones_by_cmid() -> None:
    read = (
        _m(3, "Lezione 3"),
        _m(1, "Lezione 1"),
        _m(2, "Soluzioni [no-ai]"),
        _m(4, visible=False),
    )
    assert eligible_modules(read, TAG) == (
        EligibleModule(1, "Lezione 1"),
        EligibleModule(3, "Lezione 3"),
    )


def test_a_duplicate_cmid_is_eligible_only_if_every_copy_is() -> None:
    read = (_m(5, "A"), _m(5, "A", conditions=True))
    assert eligible_modules(read, TAG) == ()
    assert eligible_modules((_m(5, "A"), _m(5, "A")), TAG) == (EligibleModule(5, "A"),)


def test_an_empty_read_has_no_eligible_module() -> None:
    assert eligible_modules((), TAG) == ()


def test_the_setting_defaults_to_the_ingest_tag_and_refuses_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert VektraAssessSettings().rag_opt_out_tag == "[no-ai]"
    for value in ("", "   "):
        monkeypatch.setenv("VEKTRA_ASSESS_RAG_OPT_OUT_TAG", value)
        with pytest.raises(ValidationError, match="must not be empty"):
            VektraAssessSettings()
