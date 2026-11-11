"""The kit's own mechanics, without a provider (the runs on providers live with them)."""

from __future__ import annotations

import pytest

from vektra_shared.lms import (
    HtmlExtractionProfile,
    LmsSite,
    Readable,
    ReviewHtml,
    UnreadableReason,
)
from vektra_testkit.lms_conformance import kit


def test_the_member_table_is_exactly_the_protocol() -> None:
    """The kit calls every one of the 23 members, and nothing that is not one."""
    covered = set(kit.READING_MEMBERS) | kit.OTHER_MEMBERS
    assert covered == kit.protocol_members()
    assert len(covered) == 23
    assert not set(kit.READING_MEMBERS) & kit.OTHER_MEMBERS


def test_walk_text_reaches_nested_values_but_not_review_html() -> None:
    profile = HtmlExtractionProfile({"question_text": "qt"}, frozenset({"x"}), "p1")
    value = Readable(
        (LmsSite("s", "https://lms.example.org"), ReviewHtml("<b>HIDDEN</b>", profile)),
    )
    text = "\n".join(kit.walk_text(value))
    assert "https://lms.example.org" in text and "qt" in text and "p1" in text
    assert "HIDDEN" not in text


def test_check_protocol_refuses_a_partial_provider() -> None:
    class Partial:
        name = "partial"

    with pytest.raises(AssertionError, match="does not implement"):
        kit.check_protocol(Partial())  # type: ignore[arg-type]


def test_the_fault_modes_name_their_reason() -> None:
    assert kit.TransportMode.TIMEOUT.expected_reason is UnreadableReason.TIMEOUT
    assert kit.TransportMode.RAISING.expected_reason is None  # any Unreadable will do
    assert kit.TransportMode.RECORDED not in kit.FAULT_MODES
