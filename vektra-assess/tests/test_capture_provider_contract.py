"""Capture on what the provider declares (PR 2a-2 review, findings 2 and 6), on the fake.

- Extraction follows the kinds the provider's profile is proved for: Vektra's list alone is
  not a safety proof for another LMS's markup.
- A file-ownership refusal is recognised by the Protocol's neutral codes, so the
  ``assess_file_rejected`` audit works for any provider.
"""

from __future__ import annotations

import dataclasses

from support.fake_lms import (
    ASSIGNMENT,
    ATTEMPT,
    PROFILE,
    USER,
    FakeLmsProvider,
    default_world,
)
from test_capture_fake import _assign, _quiz

from vektra_assess.context.capture.capture import Built
from vektra_assess.context.qtypes import EXTRACTION_KINDS, extractable
from vektra_assess.parser_protocol import FileStatus
from vektra_shared.lms import (
    FILE_NOT_IN_SUBMISSION,
    SUBMISSION_MISMATCH,
    QuestionKind,
    ReviewHtml,
)


def _world_with_profile_kinds(kinds: frozenset[QuestionKind]) -> object:
    w = default_world()
    review = w.reviews[ATTEMPT]
    item = review.items[0]
    assert item.html is not None
    profile = dataclasses.replace(PROFILE, kinds=kinds)
    item = dataclasses.replace(item, html=ReviewHtml(item.html.html, profile))
    w.reviews[ATTEMPT] = dataclasses.replace(review, items=(item,))
    return w


async def test_a_kind_the_profile_does_not_declare_is_not_extracted() -> None:
    """The item is multiple choice, in Vektra's list; the provider proved its profile for
    essays only: no sidecar call, the item is degraded (mark-and-feedback level)."""
    fake = FakeLmsProvider(_world_with_profile_kinds(frozenset({QuestionKind.ESSAY})))  # type: ignore[arg-type]
    out, parser = await _quiz(fake)
    assert isinstance(out, Built), out
    (item,) = out.snapshot.quiz_items
    assert item.qtype == QuestionKind.MULTIPLE_CHOICE.value
    assert item.degraded and item.question_text is None
    assert "review_item" not in parser.calls


async def test_the_declared_kind_is_extracted() -> None:
    fake = FakeLmsProvider(
        _world_with_profile_kinds(frozenset({QuestionKind.MULTIPLE_CHOICE}))  # type: ignore[arg-type]
    )
    out, parser = await _quiz(fake)
    assert isinstance(out, Built)
    assert out.snapshot.quiz_items[0].question_text == "2 + 2?"
    assert parser.calls.count("review_item") == 1


def test_extraction_needs_both_lists() -> None:
    html = ReviewHtml(
        "<p/>", dataclasses.replace(PROFILE, kinds=frozenset(QuestionKind))
    )
    assert not extractable(
        QuestionKind.MATCHING, html
    )  # declared, but not in Vektra's list
    assert QuestionKind.MATCHING not in EXTRACTION_KINDS
    none = ReviewHtml("<p/>", dataclasses.replace(PROFILE, kinds=frozenset()))
    assert not extractable(
        QuestionKind.ESSAY, none
    )  # an empty declaration extracts nothing
    assert not extractable(QuestionKind.ESSAY, None)


async def _rejected_audit(fake: FakeLmsProvider) -> list[tuple[str, str | None]]:
    out, _ = await _assign(fake)
    assert isinstance(out, Built), out
    assert all(f.status is FileStatus.CORRUPT for f in out.snapshot.files)
    return [
        (a.event, a.reason_code) for a in out.audit if a.event == "assess_file_rejected"
    ]


async def test_a_submission_mismatch_is_audited_with_the_neutral_code() -> None:
    w = default_world()
    sub = w.submissions[(ASSIGNMENT, USER)]
    # the file re-check sees another submission id than the one capture selected
    fake = FakeLmsProvider(w)
    real = fake.submission_file

    async def other_submission(site, assignment_id, ref, owner, submission_id):  # type: ignore[no-untyped-def]
        return await real(site, assignment_id, ref, owner, (sub.submission_id or 0) + 1)

    fake.submission_file = other_submission  # type: ignore[method-assign]
    assert await _rejected_audit(fake) == [
        ("assess_file_rejected", SUBMISSION_MISMATCH)
    ]


async def test_a_file_outside_the_submission_is_audited_with_the_neutral_code() -> None:
    w = default_world()
    sub = w.submissions[(ASSIGNMENT, USER)]
    ref = sub.files[0]
    # capture lists a file the submission does not hold (the provider's own re-check refuses)
    moved = dataclasses.replace(ref, locator="fake://file/elsewhere")
    w.files[moved.locator] = b"x"
    fake = FakeLmsProvider(w)
    real_submission = fake.submission

    async def listing_another_file(site, assignment_id, user_id):  # type: ignore[no-untyped-def]
        got = await real_submission(site, assignment_id, user_id)
        return dataclasses.replace(
            got, value=dataclasses.replace(got.value, files=(moved,))
        )

    fake.submission = listing_another_file  # type: ignore[method-assign]
    assert await _rejected_audit(fake) == [
        ("assess_file_rejected", FILE_NOT_IN_SUBMISSION)
    ]
