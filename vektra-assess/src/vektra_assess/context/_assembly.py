"""The only constructor of ``ContextBundle`` (import-linter contract (d): private to the package).

``assemble`` projects the snapshot field by field through the frozen policy. It can only NARROW:
every value comes from the snapshot, and the policy can only remove. So a toggle switched ON after
a Start captured with it OFF yields no solution: the snapshot has none, and the frozen policy says
OFF anyway.
"""

from __future__ import annotations

from vektra_assess.context import bundle as _bundle
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import FrozenPolicy
from vektra_assess.context.snapshot import Snapshot, content_hash

_SENTINEL = object()
if not _bundle._KEY:
    _bundle._KEY.append(_SENTINEL)
else:  # pragma: no cover - a second import of this module would be a bug
    _SENTINEL = _bundle._KEY[0]


def assemble(snapshot: Snapshot, frozen: FrozenPolicy) -> ContextBundle:
    p = frozen.policy
    h = snapshot.header
    # the frozen flags must be the ones the snapshot was captured under (BR-008)
    if frozen.inputs.flags != h.flags:
        raise ValueError("frozen policy does not belong to this snapshot")
    return ContextBundle(
        _sentinel=_SENTINEL,
        activity_kind=h.activity_kind,
        attempt_number=h.attempt_number,
        attempt_count=h.attempt_count,
        grade_method=h.grade_method,
        quiz_items=snapshot.quiz_items,
        criteria=snapshot.criteria,
        overall=snapshot.overall,
        submission=snapshot.submission,
        files=snapshot.files,
        solution_text=snapshot.solution_text if p.solution else None,
        pasted_rubric=snapshot.pasted_rubric if p.pasted_rubric else None,
        solution_shown=p.solution and snapshot.solution_text is not None,
        html_structured_only=snapshot.exclusion.html_structured_only,
        content_hash=content_hash(snapshot),
    )
