"""BR-008: compare the grade read at close with the snapshot's. Pure; never refreshes anything.

Called by plan 10 when the transcript header is built. A regrade never re-binds the session and
the snapshot is never rewritten: the result is only a flag for the header ("voto modificato dopo
l'avvio" with both values, or the withdrawal note). The re-read grade is never written back.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vektra_assess.context.snapshot import GradeKind, Snapshot
from vektra_shared.lms import Readable, Unreadable


@dataclass(frozen=True)
class GradeReading:
    """The grade as Moodle shows it to the student now (from the release-gated reader)."""

    released: bool
    grade: str | None  # canonical decimal string (points)
    scale_label: str | None = None  # scale grades


class RegradeKind(StrEnum):
    NONE = "none"
    CHANGED = "changed"
    WITHDRAWN = "withdrawn"  # no longer released to the student
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class RegradeFlag:
    kind: RegradeKind
    old: str | None = None
    new: str | None = None


def _shown(kind: GradeKind, grade: str | None, scale_label: str | None) -> str | None:
    return scale_label if kind is GradeKind.SCALE else grade


def compare_grade(
    snapshot: Snapshot, current: Readable[GradeReading] | Unreadable
) -> RegradeFlag:
    kind = snapshot.overall.grade_kind
    old = _shown(kind, snapshot.overall.grade, snapshot.overall.scale_label)
    if isinstance(current, Unreadable):
        return RegradeFlag(RegradeKind.UNREADABLE, old=old)
    now = current.value
    if not now.released:
        return RegradeFlag(RegradeKind.WITHDRAWN, old=old)
    new = _shown(kind, now.grade, now.scale_label)
    if new != old:
        return RegradeFlag(RegradeKind.CHANGED, old=old, new=new)
    return RegradeFlag(RegradeKind.NONE, old=old, new=new)
