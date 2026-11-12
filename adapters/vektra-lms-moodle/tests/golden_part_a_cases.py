"""The Part A golden cases (REQ-094 AC1): recorded captures plus synthetic variants.

Every case is a (snapshot, frozen policy, session facts) triple. Recorded cases come from
``capture_quiz`` / ``capture_assign`` on the 4.3 and 5.2 recordings at a fixed ``now``; synthetic
ones vary a recorded snapshot (file statuses, toggle ON, hidden marks, later sessions).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from assess_moodle_support import BRANCHES, uid
from test_context_capture import OFF, InProcessParser, _env

from vektra_assess.context.capture.capture import (
    AssignTarget,
    Built,
    Capabilities,
    QuizTarget,
    TeacherConfigView,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.part_a import SessionFacts
from vektra_assess.context.policy import FrozenPolicy, PolicyInputs, freeze
from vektra_assess.context.snapshot import FileEntry, FrozenFlags, MarksHidden, Snapshot
from vektra_assess.parser_protocol import FileStatus

NOW = 1_800_000_000
ON = TeacherConfigView(
    show_solution=True,
    rubric_source="pasted",
    solution_text="Soluzione del docente: la risposta attesa è 42.",
    pasted_rubric="Rubrica: chiarezza (0-5), metodo (0-5).",
)

QUIZZES = [
    ("review_all", "stu1"), ("review_all", "stu2"), ("review_all", "stu3"), ("review_mixed", "stu1"),
    ("gm_highest", "stu1"), ("gm_first", "stu1"), ("gm_last", "stu1"), ("gm_average", "stu1"),
    ("essay", "stu1"), ("types", "stu1"),
]  # fmt: skip
ASSIGNS = [
    ("rubric_vis", "stu1"),
    ("rubric_hidden", "stu1"),
    ("scale", "stu1"),
    ("simple", "stu1"),
    ("wf", "stu1"),
]


@dataclass(frozen=True)
class Case:
    name: str
    snapshot: Snapshot
    frozen: FrozenPolicy
    facts: SessionFacts


async def _quiz(branch: str, name: str, user: str, teacher: TeacherConfigView) -> Built:
    _replay, readers, m = _env(branch)
    q = m["quizzes"][name]
    out = await capture_quiz(
        QuizTarget(m["courseid"], q["id"], q["cmid"], uid(branch, user)),
        readers, InProcessParser(), teacher, Capabilities(), lms_release=m["release"], now=NOW,
    )  # fmt: skip
    assert isinstance(out, Built), (branch, name, user, out)
    return out


async def _assign(
    branch: str, name: str, user: str, teacher: TeacherConfigView
) -> Built:
    _replay, readers, m = _env(branch)
    a = m["assigns"][name]
    out = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(branch, user)),
        readers, InProcessParser(), teacher, Capabilities(), lms_release=m["release"], now=NOW,
    )  # fmt: skip
    assert isinstance(out, Built), (branch, name, user, out)
    return out


def _refreeze(snap: Snapshot, frozen: FrozenPolicy, **review: Any) -> FrozenPolicy:
    inputs: dict[str, Any] = frozen.inputs.model_dump()
    if review:
        inputs["review"] = review
    return freeze(
        PolicyInputs.model_validate(inputs | {"flags": snap.header.flags.model_dump()})
    )


FILES_ALL = (
    FileEntry(label="F1", filename="relazione.pdf", status=FileStatus.READ, text="Testo letto.", pages_total=3),
    FileEntry(label="F2", filename="lungo.pdf", status=FileStatus.READ_PARTIAL, text="Prime pagine.", upto_page=50, pages_total=80),
    FileEntry(label="F3", filename="slide.pptx", status=FileStatus.UNSUPPORTED_FORMAT),
    FileEntry(label="F4", filename="enorme.pdf", status=FileStatus.TOO_LARGE),
    FileEntry(label="F5", filename="scansione.pdf", status=FileStatus.NO_TEXT, pages_total=2),
    FileEntry(label="F6", filename="protetto.pdf", status=FileStatus.PROTECTED),
    FileEntry(label="F7", filename="rotto.docx", status=FileStatus.CORRUPT),
    FileEntry(label="F8", filename="<script>alert(1)</script>.txt", status=FileStatus.READ, text="x"),
)  # fmt: skip


async def cases() -> list[Case]:
    out: list[Case] = []
    for b in BRANCHES:
        tag = b.replace(".", "")
        for name, user in QUIZZES:
            built = await _quiz(b, name, user, OFF)
            out.append(
                Case(
                    f"m{tag}_quiz_{name}_{user}",
                    built.snapshot,
                    built.frozen_policy,
                    SessionFacts(1, 30),
                )
            )
        for name, user in ASSIGNS:
            built = await _assign(b, name, user, OFF)
            out.append(
                Case(
                    f"m{tag}_assign_{name}_{user}",
                    built.snapshot,
                    built.frozen_policy,
                    SessionFacts(1, 45),
                )
            )
        # toggle ON: the solution label, pasted rubric
        on_q = await _quiz(b, "review_mixed", "stu1", ON)
        out.append(
            Case(
                f"m{tag}_quiz_review_mixed_toggle_on",
                on_q.snapshot,
                on_q.frozen_policy,
                SessionFacts(1, 30),
            )
        )
        on_a = await _assign(b, "simple", "stu1", ON)
        out.append(
            Case(
                f"m{tag}_assign_simple_toggle_on",
                on_a.snapshot,
                on_a.frozen_policy,
                SessionFacts(1, 45),
            )
        )
        # every file status, one file per status, on a recorded assignment
        simple = await _assign(b, "simple", "stu1", OFF)
        for f in FILES_ALL:
            snap = simple.snapshot.model_copy(update={"files": (f,)})
            out.append(
                Case(
                    f"m{tag}_assign_file_{f.label.lower()}_{f.status.value.lower()}",
                    snap,
                    simple.frozen_policy,
                    SessionFacts(1, 45),
                )
            )
        snap = simple.snapshot.model_copy(update={"files": FILES_ALL})
        out.append(
            Case(
                f"m{tag}_assign_files_all",
                snap,
                simple.frozen_policy,
                SessionFacts(2, 60),
            )
        )
        # scale grade with files
        scale = await _assign(b, "scale", "stu1", OFF)
        snap = scale.snapshot.model_copy(update={"files": FILES_ALL[:3]})
        out.append(
            Case(
                f"m{tag}_assign_scale_with_files",
                snap,
                scale.frozen_policy,
                SessionFacts(1, 45),
            )
        )
        # hidden marks: max only, then nothing (built from a recorded quiz)
        base = await _quiz(b, "review_all", "stu1", OFF)
        max_only = tuple(
            i.model_copy(
                update={
                    "mark": None,
                    "state": None,
                    "status": None,
                    "marks_hidden": MarksHidden.REVIEW_OPTIONS,
                }
            )
            for i in base.snapshot.quiz_items
        )
        snap = base.snapshot.model_copy(update={"quiz_items": max_only})
        out.append(
            Case(
                f"m{tag}_quiz_marks_max_only",
                snap,
                base.frozen_policy,
                SessionFacts(1, 30),
            )
        )
        hidden = tuple(i.model_copy(update={"max_mark": None}) for i in max_only)
        snap = base.snapshot.model_copy(update={"quiz_items": hidden})
        out.append(
            Case(
                f"m{tag}_quiz_marks_hidden",
                snap,
                base.frozen_policy,
                SessionFacts(1, 30),
            )
        )
        # later sessions and longer durations
        out.append(
            Case(
                f"m{tag}_quiz_session_3_extra_time",
                base.snapshot,
                base.frozen_policy,
                SessionFacts(
                    3,
                    90,
                    previous_context="Riga di contesto della sessione precedente (piano 8).",
                ),
            )
        )
    return out


def flags_of(snap: Snapshot) -> FrozenFlags:
    return snap.header.flags


def iter_names(cs: list[Case]) -> Iterator[str]:
    return (c.name for c in cs)
