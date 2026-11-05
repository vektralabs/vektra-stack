"""BR-008 regrade flag: pure, and the snapshot is never touched."""

from __future__ import annotations

from test_context_snapshot import make

from vektra_assess.context.regrade import (
    GradeReading,
    RegradeFlag,
    RegradeKind,
    compare_grade,
)
from vektra_assess.context.snapshot import GradeKind, OverallItem, content_hash
from vektra_assess.readable import Readable, Unreadable, UnreadableReason


def test_flags() -> None:
    s = make()  # grade 7.5 / 10
    before = content_hash(s)
    assert compare_grade(s, Readable(GradeReading(True, "7.5"))) == RegradeFlag(
        RegradeKind.NONE, "7.5", "7.5"
    )
    assert compare_grade(s, Readable(GradeReading(True, "8"))) == RegradeFlag(
        RegradeKind.CHANGED, "7.5", "8"
    )
    assert compare_grade(s, Readable(GradeReading(False, None))) == RegradeFlag(
        RegradeKind.WITHDRAWN, "7.5"
    )
    assert compare_grade(s, Unreadable(UnreadableReason.TIMEOUT)) == RegradeFlag(
        RegradeKind.UNREADABLE, "7.5"
    )
    assert content_hash(s) == before  # never refreshed


def test_scale_grades_compare_labels() -> None:
    s = make().model_copy(
        update={
            "overall": OverallItem(
                grade_kind=GradeKind.SCALE,
                grade=None,
                grade_max=None,
                scale_label="Buono",
                grade_read_at=1,
            )
        }
    )
    assert (
        compare_grade(s, Readable(GradeReading(True, None, "Ottimo"))).kind
        is RegradeKind.CHANGED
    )
    assert (
        compare_grade(s, Readable(GradeReading(True, None, "Buono"))).kind
        is RegradeKind.NONE
    )
