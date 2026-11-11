"""The kit's own mechanics, without a provider (the runs on providers live with them)."""

from __future__ import annotations

import pytest

from lms_conformance import kit
from vektra_shared.lms import (
    HtmlExtractionProfile,
    LmsSite,
    Readable,
    ReviewHtml,
    UnreadableReason,
)


def test_the_member_table_is_exactly_the_protocol() -> None:
    """The kit calls every one of the 24 members, and nothing that is not one."""
    covered = set(kit.READING_MEMBERS) | kit.OTHER_MEMBERS
    assert covered == kit.protocol_members()
    assert len(covered) == 24
    assert not set(kit.READING_MEMBERS) & kit.OTHER_MEMBERS


def test_walk_text_reaches_nested_values() -> None:
    profile = HtmlExtractionProfile({"question_text": "qt"}, frozenset({"x"}), "p1")
    value = Readable(
        (LmsSite("s", "https://lms.example.org"), ReviewHtml("<b>x</b>", profile)),
    )
    # exact membership in the walked values, not a substring of their join
    walked = list(kit.walk_text(value))
    assert {"https://lms.example.org", "qt", "p1"} <= set(walked)


def test_walk_text_reaches_numbers() -> None:
    """Review of #203: an unreleased grade is a number; a walk over strings only never sees
    a numeric canary."""
    text = "\n".join(kit.walk_text(Readable((987654.321, 4242))))
    assert "987654.321" in text and "4242" in text


def test_review_html_is_walked_through_its_own_profile() -> None:
    """Review of #203: the HTML is walked, as the profile leaves it. What the profile keeps is
    checked; what it drops (the teacher-visible right answer) and what lies outside every kept
    block is not what the vertical receives."""
    profile = HtmlExtractionProfile(
        {"question_text": "qt", "response": "resp"}, frozenset({"right"}), "p1"
    )
    html = (
        '<div class="qt">KEPT-Q <span class="right">DROPPED-IN-BLOCK</span></div>'
        '<div class="resp"><p>KEPT-R</p></div>'
        '<div class="right">DROPPED-OUTSIDE</div><div>NOT-KEPT</div>'
    )
    text = "\n".join(kit.walk_text(ReviewHtml(html, profile)))
    assert "KEPT-Q" in text and "KEPT-R" in text
    for gone in ("DROPPED-IN-BLOCK", "DROPPED-OUTSIDE", "NOT-KEPT"):
        assert gone not in text


def test_check_protocol_refuses_a_partial_provider() -> None:
    class Partial:
        name = "partial"

    with pytest.raises(AssertionError, match="does not implement"):
        kit.check_protocol(Partial())  # type: ignore[arg-type]


def test_the_fault_modes_name_their_reason() -> None:
    assert kit.TransportMode.TIMEOUT.expected_reason is UnreadableReason.TIMEOUT
    assert kit.TransportMode.RAISING.expected_reason is None  # any Unreadable will do
    assert kit.TransportMode.RECORDED not in kit.FAULT_MODES
