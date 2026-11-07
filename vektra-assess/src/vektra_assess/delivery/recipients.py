"""Transcript recipients (REQ-082): one selection rule for generation and for ``/state``.

v1 ships Moodle-resolved graders only (free-text overrides are not built, REQ-082 SHOULD). The
rule, until plan assess-teacher-ops stores the teacher's own choice (``configured``): at most
``MAX_RECIPIENTS`` users holding the grading capability in the course, the configured ones that
still hold it first, then the course's graders by ascending user id; the student is never a
recipient of their own transcript. Names are for display only (the pre-session screen names the
recipients by role and name); no address ever passes through here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

MAX_RECIPIENTS = 3
ROLE_GRADER = "grader"


class GraderLike(Protocol):
    """A user holding the grading capability (``moodle.domain.GraderView`` fits; this package
    must not import ``vektra_assess.moodle``, import-linter contract (c))."""

    @property
    def userid(self) -> int: ...

    @property
    def fullname(self) -> str: ...


@dataclass(frozen=True)
class Recipient:
    userid: int
    name: str
    role: str = ROLE_GRADER


def select_recipients(
    graders: Sequence[GraderLike],
    *,
    student_userid: int,
    configured: Sequence[int] = (),
) -> tuple[Recipient, ...]:
    holders = {g.userid: g for g in graders if g.userid != student_userid}
    order = [u for u in dict.fromkeys(configured) if u in holders]
    order += [u for u in sorted(holders) if u not in order]
    return tuple(Recipient(u, holders[u].fullname) for u in order[:MAX_RECIPIENTS])


def state_view(recipients: Sequence[Recipient]) -> list[dict[str, str]]:
    """The ``/state`` ``pre_session.recipients`` items: role and name, nothing else."""
    return [{"role": r.role, "name": r.name} for r in recipients]
