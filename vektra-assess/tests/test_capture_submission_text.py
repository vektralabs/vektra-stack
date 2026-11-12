"""The submission's online text and files at capture (REQ-101 (c)), on stub readers and parser.

``_files`` and ``_onlinetext`` decide, whatever the LMS, what of a submission reaches the
snapshot: the file-count and size caps, a file of no selected submission, a refused
ownership check (audited), and the total text budget, which keeps what fits as partial and never
presents a cut text as complete. Until 2a-4 these branches ran only over the Moodle provider's
recordings (now in ``adapters/``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from vektra_assess.context.capture.capture import (
    AssignTarget,
    CaptureLimits,
    _files,
    _onlinetext,
)
from vektra_assess.parser_protocol import Extracted, FileStatus
from vektra_shared.lms import (
    FILE_NOT_IN_SUBMISSION,
    Readable,
    Submission,
    SubmissionFileRef,
    Unreadable,
    UnreadableReason,
)

TARGET = AssignTarget(courseid=2, assignid=7, cmid=70, userid=5)


def _ref(name: str, size: int = 100) -> SubmissionFileRef:
    return SubmissionFileRef(name, "/", size, "application/pdf", f"loc-{name}", 1)


def _submission(*refs: SubmissionFileRef, submission_id: int | None = 42) -> Submission:
    return Submission(
        assignment_id=7,
        user_id=5,
        submission_id=submission_id,
        team_submission=False,
        state="submitted",
        time_modified=1,
        online_text=None,
        files=tuple(refs),
        grading_status="graded",
        graded=True,
        extension_due_date=None,
        feedback=None,
    )


@dataclass
class Readers:
    """``submission_file`` only; a name in ``refused`` answers the ownership refusal."""

    refused: tuple[str, ...] = ()

    async def submission_file(
        self, assignid: int, ref: SubmissionFileRef, userid: int, submission_id: int
    ) -> Any:
        if ref.filename in self.refused:
            return Unreadable(UnreadableReason.UNKNOWN_SHAPE, FILE_NOT_IN_SUBMISSION)
        return Readable(ref.filename.encode())


@dataclass
class Parser:
    """Answers each file with ``texts[filename]`` (status READ), or ``status`` when given."""

    texts: dict[str, str]
    statuses: dict[str, FileStatus] | None = None
    html: Extracted | None = None

    async def parse_file(self, data: bytes, ext_hint: str = "") -> Extracted:
        name = data.decode()
        status = (self.statuses or {}).get(name, FileStatus.READ)
        return Extracted(status, self.texts.get(name, ""), pages_total=3)

    async def sanitize_html(self, html: bytes) -> Extracted:
        assert self.html is not None
        return self.html


async def _run(
    sub: Submission, parser: Parser, readers: Readers | None = None, **limits: Any
):
    audit: list[Any] = []
    out = await _files(
        TARGET, sub, readers or Readers(), parser, CaptureLimits(**limits), audit
    )
    return [(f.label, f.status, f.text, f.upto_page) for f in out], audit


async def test_the_file_caps_are_applied_before_anything_is_read() -> None:
    sub = _submission(_ref("a.pdf"), _ref("b.pdf", size=10_000), _ref("c.pdf"))
    files, _ = await _run(
        sub, Parser({"a.pdf": "A", "c.pdf": "C"}), max_files=2, max_file_bytes=1_000
    )
    assert files == [
        ("F1", FileStatus.READ, "A", None),
        ("F2", FileStatus.TOO_LARGE, "", None),
        ("F3", FileStatus.OVER_MAX_FILES, "", None),
    ]


async def test_a_file_of_no_selected_submission_is_not_read() -> None:
    files, _ = await _run(_submission(_ref("a.pdf"), submission_id=None), Parser({}))
    assert files == [("F1", FileStatus.CORRUPT, "", None)]


async def test_a_refused_ownership_is_audited_and_the_file_not_read() -> None:
    files, audit = await _run(
        _submission(_ref("a.pdf")), Parser({"a.pdf": "A"}), Readers(refused=("a.pdf",))
    )
    assert files == [("F1", FileStatus.CORRUPT, "", None)]
    assert [(e.event, e.reason_code) for e in audit] == [
        ("assess_file_rejected", FILE_NOT_IN_SUBMISSION)
    ]


async def test_the_total_text_budget_keeps_what_fits_as_partial() -> None:
    """A file past the budget keeps what fits as partial, with no page claimed (the cut is not
    the parser's page); one that gets nothing at all is too large, not "no text"."""
    sub = _submission(_ref("a.pdf"), _ref("b.pdf"), _ref("c.pdf"))
    files, _ = await _run(
        sub,
        Parser({"a.pdf": "x" * 6, "b.pdf": "y" * 6, "c.pdf": "z"}),
        total_text_chars=10,
    )
    assert files == [
        ("F1", FileStatus.READ, "x" * 6, None),
        ("F2", FileStatus.READ_PARTIAL, "y" * 4, None),
        ("F3", FileStatus.TOO_LARGE, "", None),
    ]


async def test_a_read_file_with_only_whitespace_has_no_text() -> None:
    files, _ = await _run(_submission(_ref("a.pdf")), Parser({"a.pdf": "  \n "}))
    assert files == [("F1", FileStatus.NO_TEXT, "", None)]


async def test_a_parser_refusal_keeps_its_status_and_no_text() -> None:
    files, _ = await _run(
        _submission(_ref("a.pdf")),
        Parser({"a.pdf": "secret"}, statuses={"a.pdf": FileStatus.PROTECTED}),
    )
    assert files == [("F1", FileStatus.PROTECTED, "", None)]


# --- the online text ----------------------------------------------------------------------------


@pytest.mark.parametrize("html", [None, "", "   "])
async def test_no_online_text_is_no_submission_item(html: str | None) -> None:
    assert await _onlinetext(Parser({}), html, CaptureLimits()) is None


async def test_a_blank_text_once_sanitized_is_no_submission_item() -> None:
    parser = Parser({}, html=Extracted(FileStatus.NO_TEXT))
    assert await _onlinetext(parser, "<p></p>", CaptureLimits()) is None


async def test_an_unreadable_online_text_is_said_to_be() -> None:
    parser = Parser({}, html=Extracted(FileStatus.CORRUPT))
    item = await _onlinetext(parser, "<p>x</p>", CaptureLimits())
    assert item is not None and item.unreadable and item.onlinetext is None


async def test_the_online_text_is_capped_by_both_budgets_and_marked_truncated() -> None:
    parser = Parser({}, html=Extracted(FileStatus.READ, "abcdefgh"))
    item = await _onlinetext(
        parser,
        "<p>x</p>",
        replace(CaptureLimits(), onlinetext_chars=6, total_text_chars=4),
    )
    assert item is not None and (item.onlinetext, item.truncated) == ("abcd", True)
    partial = Parser({}, html=Extracted(FileStatus.READ_PARTIAL, "abc"))
    item = await _onlinetext(partial, "<p>x</p>", CaptureLimits())
    assert item is not None and (item.onlinetext, item.truncated) == ("abc", True)
