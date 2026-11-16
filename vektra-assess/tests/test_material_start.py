"""The course-material flag is decided at Start and frozen (FEAT-028, REQ-077 amendment).

The pre-flight reads the operator flag and the activity's RAG ON + attestation; the capture
freezes the result into the snapshot's flags (BR-008), so a session keeps the RAG setting it
started with, whatever the teacher saves later.
"""

from __future__ import annotations

from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.capture.capture import TeacherConfigView, _flags
from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.start_source import teacher_view


def test_the_teacher_view_carries_the_start_decision() -> None:
    assert teacher_view({}, "quiz", None).course_material is False
    assert teacher_view({}, "quiz", None, course_material=True).course_material is True


def test_the_frozen_flags_carry_it() -> None:
    on = TeacherConfigView(
        show_solution=False, rubric_source="none", course_material=True
    )
    off = TeacherConfigView(show_solution=False, rubric_source="none")
    assert _flags(on).course_material is True
    assert _flags(off).course_material is False


def test_a_start_target_defaults_to_no_material() -> None:
    import uuid

    t = StartTarget(uuid.uuid4(), uuid.uuid4(), 1, 1, None, "quiz", 1, {})
    assert t.course_material is False


def test_the_bundle_of_a_session_is_its_frozen_flag() -> None:
    s = make()
    on = s.model_copy(
        update={
            "header": s.header.model_copy(
                update={
                    "flags": s.header.flags.model_copy(update={"course_material": True})
                }
            )
        }
    )
    assert assemble_context(on, _frozen(on)).course_material is True
    assert assemble_context(s, _frozen(s)).course_material is False
