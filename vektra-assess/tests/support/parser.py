"""The parser sidecar's dispatch in-process, for the capture tests (no socket).

``InProcessParser`` gives the real file and HTML statuses (``child.run``) and parses the
``review_item`` reply the way ``ParserClient`` does, with the provider's profile over the wire
form the sidecar validates (ADR-0033 D4). ``OFF`` is the teacher config that shows nothing.
Used by the vertical's capture tests and by the Moodle provider's joint suites.
"""

from __future__ import annotations

from typing import Any

from vektra_assess.context.capture.capture import TeacherConfigView
from vektra_assess.parser_protocol import ReviewProfile
from vektra_assess.parser_sidecar.child import run
from vektra_shared.lms import HtmlExtractionProfile

OFF = TeacherConfigView(show_solution=False, rubric_source="none")


class InProcessParser:
    """The sidecar's dispatch without the socket (same statuses, same text)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.profiles: list[str] = []

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Any:
        self.calls.append("parse")
        return run("parse", data, ext_hint, max_pages=50, max_chars=200_000)

    async def sanitize_html(self, html: bytes) -> Any:
        self.calls.append("sanitize_html")
        return run("sanitize_html", html, "html", max_pages=1, max_chars=200_000)

    async def extract_review_item(
        self, html: bytes, profile: HtmlExtractionProfile
    ) -> tuple[str | None, str | None, bool]:
        """The real client's parsing of the sidecar's reply, without the socket: the
        provider's profile goes over the wire form the sidecar validates (ADR-0033 D4)."""
        import json

        self.calls.append("review_item")
        self.profiles.append(profile.profile_version)
        res = run(
            "review_item",
            html,
            "html",
            max_pages=1,
            max_chars=200_000,
            profile=wire_profile(profile),
        )
        doc = json.loads(res.text)
        return doc["question_text"], doc["response"], doc["truncated"]


def wire_profile(profile: HtmlExtractionProfile) -> ReviewProfile:
    """What ``ParserClient`` sends for ``profile`` (and the sidecar re-validates)."""
    return ReviewProfile.build(
        profile.keep, profile.drop_classes, profile.profile_version
    )
