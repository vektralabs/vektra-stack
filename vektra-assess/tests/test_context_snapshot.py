"""Snapshot allowlist models, canonical JSON and content hash (BR-015)."""

from __future__ import annotations

import json
import unicodedata

import pytest
from pydantic import ValidationError

from vektra_assess.context.snapshot import (
    ActivityKind,
    ExclusionRecord,
    FileEntry,
    FrozenFlags,
    GradeKind,
    GradeMethod,
    OverallItem,
    QuizItem,
    Snapshot,
    SnapshotHeader,
    canonical_bytes,
    content_hash,
    from_canonical_bytes,
    mark_str,
)
from vektra_assess.parser_protocol import FileStatus


def make(text: str = "risposta", **kw: object) -> Snapshot:
    return Snapshot(
        header=SnapshotHeader(
            activity_kind=ActivityKind.QUIZ,
            moodle_release="5.2",
            attempt_number=2,
            attempt_count=4,
            grade_method=GradeMethod.HIGHEST,
            review_options_closed=True,
            grade_released=True,
            grade_modified_at=1_700_000_000,
            captured_at=1_700_000_100,
            flags=FrozenFlags(show_solution=False, rubric_source="pasted"),
        ),
        quiz_items=(
            QuizItem(
                label="1",
                qtype="multichoice",
                discussable=True,
                state="gradedright",
                status="Corretta",
                mark="1",
                max_mark="1",
            ),
            QuizItem(
                label="2",
                qtype="essay",
                discussable=True,
                state="mangrpartial",
                status=None,
                mark="2.5",
                max_mark="5",
                review_text=text,
            ),
        ),
        overall=OverallItem(
            grade_kind=GradeKind.POINTS,
            grade="7.5",
            grade_max="10",
            grade_read_at=1_700_000_100,
        ),
        exclusion=ExclusionRecord(
            policy_version=1,
            excluded_paths=("q1.right_answer", "solution_text"),
            html_structured_only=True,
            rubric_descriptors_excluded=True,
            solution_shown=False,
        ),
        **kw,  # type: ignore[arg-type]
    )


def test_hash_is_stable_and_changes_with_content() -> None:
    a, b = make(), make()
    assert canonical_bytes(a) == canonical_bytes(b) and content_hash(a) == content_hash(
        b
    )
    assert content_hash(make("altra risposta")) != content_hash(a)


def test_canonical_form_is_sorted_compact_utf8_and_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "perché è così")
    data = canonical_bytes(make(decomposed))
    assert "perché è così".encode() in data  # NFC, raw UTF-8
    assert content_hash(make(decomposed)) == content_hash(make("perché è così"))
    parsed = json.loads(data)
    assert list(parsed) == sorted(parsed) and b": " not in data and b", " not in data


def test_round_trip_through_canonical_bytes() -> None:
    s = make()
    assert from_canonical_bytes(canonical_bytes(s)) == s


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (7.5, "7.5"),
        (10, "10"),
        (0.1 + 0.2, "0.3"),
        (3.33333333, "3.3333333"),
        (-0.0, "0"),
    ],
)
def test_marks_are_canonical_decimal_strings(value: float, text: str) -> None:
    assert mark_str(value) == text


@pytest.mark.parametrize("bad", ["7.50", "07", "1e1", "abc", "0.30000000000000004"])
def test_non_canonical_marks_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        QuizItem(
            label="1",
            qtype="truefalse",
            discussable=True,
            state=None,
            status=None,
            mark=bad,
            max_mark="1",
        )


def test_a_float_mark_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QuizItem(
            label="1",
            qtype="truefalse",
            discussable=True,
            state=None,
            status=None,
            mark=7.5,
            max_mark="1",
        )  # type: ignore[arg-type]


def test_unknown_fields_are_rejected_everywhere() -> None:
    raw = json.loads(canonical_bytes(make()))
    raw["quiz_items"][0]["right_answer"] = "B"
    with pytest.raises(ValidationError, match="extra"):
        from_canonical_bytes(json.dumps(raw).encode())
    raw = json.loads(canonical_bytes(make()))
    raw["raw_moodle"] = {"html": "<p>"}
    with pytest.raises(ValidationError, match="extra"):
        from_canonical_bytes(json.dumps(raw).encode())


def test_snapshot_is_frozen() -> None:
    s = make()
    with pytest.raises(ValidationError):
        s.solution_text = "x"  # type: ignore[misc]


def test_file_text_only_with_a_read_status() -> None:
    FileEntry(label="F1", filename="a.pdf", status=FileStatus.READ, text="ok")
    with pytest.raises(ValidationError, match="READ"):
        FileEntry(
            label="F2",
            filename="b.pptx",
            status=FileStatus.UNSUPPORTED_FORMAT,
            text="leak",
        )


def test_exclusion_paths_sorted_unique_and_hash_is_over_paths() -> None:
    rec = make().exclusion
    import hashlib

    assert (
        rec.field_set_hash()
        == hashlib.sha256(b"q1.right_answer\nsolution_text").hexdigest()
    )
    with pytest.raises(ValidationError):
        ExclusionRecord(
            policy_version=1,
            excluded_paths=("b", "a"),
            html_structured_only=True,
            rubric_descriptors_excluded=True,
            solution_shown=False,
        )


def test_no_moodle_id_field_exists() -> None:
    names: set[str] = set()
    for model in (
        Snapshot,
        SnapshotHeader,
        QuizItem,
        OverallItem,
        FileEntry,
        ExclusionRecord,
    ):
        names |= set(model.model_fields)
    forbidden = {
        "id",
        "attemptid",
        "slot",
        "cmid",
        "userid",
        "quizid",
        "assignid",
        "courseid",
        "html",
    }
    assert names & forbidden == set()
