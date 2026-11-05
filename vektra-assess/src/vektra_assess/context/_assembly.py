"""The only constructor of ``ContextBundle`` (import-linter contract (d): private to the package).

``assemble`` projects the snapshot field by field through the frozen policy. It can only NARROW:
every value comes from the snapshot, and the policy can only remove. So a toggle switched ON after
a Start captured with it OFF yields no solution: the snapshot has none, and the frozen policy says
OFF anyway.
"""

from __future__ import annotations

from vektra_assess.context import bundle as _bundle
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import DisclosurePolicy, FrozenPolicy, HtmlPath
from vektra_assess.context.snapshot import (
    AssignCriterion,
    MarksHidden,
    QuizItem,
    Snapshot,
    content_hash,
)

_SENTINEL = object()
if not _bundle._KEY:
    _bundle._KEY.append(_SENTINEL)
else:  # pragma: no cover - a second import of this module would be a bug
    _SENTINEL = _bundle._KEY[0]


def _item(i: QuizItem, p: DisclosurePolicy, excluded: frozenset[str]) -> QuizItem:
    update: dict[str, object] = {}
    if not p.marks:
        update |= {"mark": None, "state": None, "status": None}
        if i.marks_hidden is None:
            update["marks_hidden"] = MarksHidden.REVIEW_OPTIONS
    if not p.max_marks:
        update["max_mark"] = None
    if i.review_text is not None:
        # the review text cannot be filtered again here: keep it only when the snapshot already
        # excluded everything this policy excludes, and the policy allows the filterable path
        required = {
            f"q{i.label}.{name}"
            for name, allowed in (
                ("right_answer", p.right_answer),
                ("specific_feedback", p.specific_feedback),
                ("general_feedback", p.general_feedback),
                ("mark", p.marks),
            )
            if not allowed
        }
        if p.html_path is not HtmlPath.FILTERABLE or not required <= excluded:
            update["review_text"] = None
    return i.model_copy(update=update) if update else i


def _criterion(c: AssignCriterion, p: DisclosurePolicy) -> AssignCriterion:
    if p.rubric_descriptors:
        return c
    return c.model_copy(
        update={
            "description": None,
            "chosen_level_text": None,
            "adjacent_level_texts": (),
        }
    )


def assemble(snapshot: Snapshot, frozen: FrozenPolicy) -> ContextBundle:
    p = frozen.policy
    h = snapshot.header
    # the frozen flags must be the ones the snapshot was captured under (BR-008)
    if frozen.inputs.flags != h.flags:
        raise ValueError("frozen policy does not belong to this snapshot")
    # capture already filtered by this policy; the projection below only re-applies it, so a
    # narrower policy paired with the snapshot still removes what it hides
    excluded = frozenset(snapshot.exclusion.excluded_paths)
    overall = snapshot.overall
    if not p.overall_feedback and overall.overall_feedback is not None:
        overall = overall.model_copy(update={"overall_feedback": None})
    return ContextBundle(
        _sentinel=_SENTINEL,
        activity_kind=h.activity_kind,
        attempt_number=h.attempt_number,
        attempt_count=h.attempt_count,
        grade_method=h.grade_method,
        quiz_items=tuple(_item(i, p, excluded) for i in snapshot.quiz_items),
        criteria=tuple(_criterion(c, p) for c in snapshot.criteria),
        overall=overall,
        submission=snapshot.submission,
        files=snapshot.files,
        solution_text=snapshot.solution_text if p.solution else None,
        pasted_rubric=snapshot.pasted_rubric if p.pasted_rubric else None,
        solution_shown=p.solution and snapshot.solution_text is not None,
        html_structured_only=snapshot.exclusion.html_structured_only,
        content_hash=content_hash(snapshot),
    )
