"""Moodle facts the transcript generator reads at close (plan 10), as a protocol and a value.

The generator lives outside ``vektra_assess.worker`` and must not import ``vektra_assess.moodle``
(import-linter contract (c)): the worker's ``MoodleStartSource.close_facts`` fills ``CloseFacts``
and the composition root hands it over as a ``CloseFactsSource``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from vektra_assess.context.regrade import GradeReading
from vektra_assess.delivery.recipients import GraderLike
from vektra_assess.readable import Readable, Unreadable


@dataclass(frozen=True)
class CloseFacts:
    """Every part is None when Moodle could not be read: the header then says so."""

    course_shortname: str | None = None
    grade: Readable[GradeReading] | Unreadable | None = None
    graders: tuple[GraderLike, ...] | None = None


class CloseFactsSource(Protocol):
    async def close_facts(
        self,
        *,
        session_id: Any,
        site_id: Any,
        cmid: int,
        student_userid: int,
        courseid_hint: int | None,
        activity_type: str,
    ) -> CloseFacts: ...
