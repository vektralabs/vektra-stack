"""LMS facts the transcript generator reads at close (plan 10), as a protocol and a value.

The generator lives outside ``vektra_assess.worker`` and holds no ``LmsProvider`` (import-linter
contract (c)): the worker's ``LmsStartSource.close_facts`` fills ``CloseFacts`` and the
composition root hands it over as a ``CloseFactsSource``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from vektra_assess.context.regrade import GradeReading
from vektra_shared.lms import Readable, Unreadable


@dataclass(frozen=True)
class CloseFacts:
    """Every part is None when the LMS could not be read: the header then says so."""

    course_shortname: str | None = None
    grade: Readable[GradeReading] | Unreadable | None = None


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
        sink: dict[str, Any] | None = None,
    ) -> CloseFacts:
        """``sink``, when given, receives each part as soon as it is read (the keys of
        ``CloseFacts``), so a caller that times out keeps what was already read."""
        ...
