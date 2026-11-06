"""The production ``StartSource``: Moodle readers + parser sidecar -> snapshot (plan 8 pre-flight).

For one STARTING session it reads, fresh and server-side only:

- the site (base URL, release) and its two ARCH-070 capability flags from the database;
- the teacher configuration view: ``flags.show_solution`` / ``flags.rubric_source`` (plan 11 owns
  the form that writes them; until then the defaults are the safe ones, solution OFF), and the
  solution text only when the toggle is ON and a sealed value exists (OQ-034);
- the activity's course and instance, resolved from the cmid inside the student's own courses
  (``resolve_activity``; the plugin's course id is only a hint, REQ-095);

then runs ``capture_quiz`` / ``capture_assign`` (plan 6), which selects the attempt server-side
(BR-012) and projects it through the disclosure policy. Client-sent attempt ids do not exist on
this path at all.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vektra_assess.context.capture.capture import (
    AssignTarget,
    Capabilities,
    CaptureParser,
    CaptureResult,
    Failed,
    FailReason,
    QuizTarget,
    TeacherConfigView,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.capture.readers import MoodleCaptureReaders
from vektra_assess.crypto.teacher_config import open_teacher_config
from vektra_assess.keys import AssessKeyring
from vektra_assess.moodle.client import MoodleWsClient, SiteRef
from vektra_assess.moodle.readers.resolve import resolve_activity
from vektra_assess.moodle.result import Unreadable
from vektra_assess.worker.preflight import StartTarget

_SITE = text("SELECT base_url, release FROM assess_sites WHERE id = :s")
_CAPS = text(
    "SELECT flag, state FROM assess_site_capabilities WHERE site_id = :s "
    "AND flag IN ('review_html_filterable', 'rubric_display_settings')"
)
_SEALED = text(
    "SELECT teacher_config_sealed FROM assess_activity_config WHERE site_id = :s AND cmid = :c"
)
SOLUTION_FIELD = "solution"


def teacher_view(
    flags: dict[str, Any], kind: str, solution_text: str | None
) -> TeacherConfigView:
    show = flags.get("show_solution") is True
    source = flags.get("rubric_source")
    if source not in ("none", "pasted", "moodle"):
        source = "moodle" if kind == "assign" else "none"
    return TeacherConfigView(
        show_solution=show,
        rubric_source=str(source),
        solution_text=solution_text if show else None,
    )


class MoodleStartSource:
    def __init__(
        self,
        client: MoodleWsClient,
        parser: CaptureParser,
        acquire: Callable[[], AbstractAsyncContextManager[AsyncConnection]],
        *,
        teacher_private: AssessKeyring | None = None,
    ) -> None:
        self._client = client
        self._parser = parser
        self._acquire = acquire
        self._teacher_private = teacher_private

    async def _context(
        self, target: StartTarget
    ) -> tuple[SiteRef, Capabilities, str | None] | None:
        async with self._acquire() as conn:
            site = (await conn.execute(_SITE, {"s": target.site_id})).first()
            if site is None or not site.release:
                return None
            caps = {
                r.flag: r.state
                for r in (await conn.execute(_CAPS, {"s": target.site_id})).all()
            }
            solution = None
            if (
                target.flags.get("show_solution") is True
                and self._teacher_private is not None
            ):
                sealed = (
                    await conn.execute(_SEALED, {"s": target.site_id, "c": target.cmid})
                ).scalar()
                if sealed is not None:
                    solution = open_teacher_config(
                        self._teacher_private,
                        bytes(sealed),
                        target.site_id,
                        target.cmid,
                        SOLUTION_FIELD,
                    )
        ref = SiteRef(str(target.site_id), str(site.base_url), str(site.release))
        capabilities = Capabilities(
            review_html_filterable=caps.get("review_html_filterable", "unreadable"),
            rubric_display_settings=caps.get("rubric_display_settings", "unreadable"),
        )
        return ref, capabilities, solution

    async def capture(self, target: StartTarget) -> CaptureResult:
        ctx = await self._context(target)
        if ctx is None:
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "site_release_unknown")
        site, caps, solution = ctx
        kind = "quiz" if target.activity_type == "quiz" else "assign"
        resolved = await resolve_activity(
            self._client,
            site,
            userid=target.student_userid,
            cmid=target.cmid,
            kind=kind,
            courseid_hint=target.courseid_hint,
        )
        if isinstance(resolved, Unreadable):
            return Failed(FailReason.POLICY_INPUT_UNREADABLE, "activity_unresolved")
        ref = resolved.value
        readers = MoodleCaptureReaders(self._client, site)
        teacher = teacher_view(target.flags, kind, solution)
        release = str(site.release)
        if kind == "quiz":
            return await capture_quiz(
                QuizTarget(
                    ref.courseid, ref.instanceid, target.cmid, target.student_userid
                ),
                readers,
                self._parser,
                teacher,
                caps,
                moodle_release=release,
            )
        return await capture_assign(
            AssignTarget(
                ref.courseid, ref.instanceid, target.cmid, target.student_userid
            ),
            readers,
            self._parser,
            teacher,
            caps,
            moodle_release=release,
        )
