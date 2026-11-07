"""Plan 10b ``delivery/message.py``: header sanitising, deterministic Message-ID, a content-free
body and the transcript bytes attached unchanged."""

from __future__ import annotations

import email
import hashlib
from datetime import UTC, datetime
from email import policy

import pytest

from vektra_assess.delivery import message as m

TID = "6f1c1d5e-0000-4000-8000-000000000001"
REF = "a" * 64
DOC = '<!DOCTYPE html><html lang="it"><body><p>Citt\u00e0 \u2013 \u2211</p></body></html>'.encode()


def _mail(**over: object) -> m.TranscriptMail:
    base: dict[str, object] = {
        "sender": "vektra@example.org",
        "recipient": "prof@unical.it",
        "course": "EAASSESS",
        "cmid": 501,
        "student_userid": 101,
        "session_no": 2,
        "filename": "EAASSESS_501_101_s2_20261107.html",
        "attachment": DOC,
        "message_id": m.message_id(TID, 1, REF, "vektra.example.org"),
        "date": datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
    }
    base.update(over)
    return m.TranscriptMail(**base)  # type: ignore[arg-type]


def _wire(mail: m.TranscriptMail) -> bytes:
    return m.build(mail).as_bytes()


@pytest.mark.parametrize(
    "payload",
    [
        "EA\r\nBcc: victim@evil.example",
        "EA\nBcc: victim@evil.example",
        "EA\rBcc: victim@evil.example",
        "EA\x00\x0b\x0c\x1b\x7f\x85Bcc: victim@evil.example",
        "EA\u2028Bcc: victim@evil.example",
    ],
)
def test_a_header_injection_in_the_course_stays_one_subject(payload: str) -> None:
    wire = _wire(_mail(course=payload))
    parsed = email.message_from_bytes(wire, policy=policy.SMTP)
    assert parsed.get_all("Bcc") is None
    assert len(parsed.get_all("Subject") or []) == 1
    head = wire.split(b"\r\n\r\n", 1)[0]
    # exactly one Subject line and no Bcc line anywhere in the header block
    lines = head.split(b"\r\n")
    assert sum(1 for ln in lines if ln.lower().startswith(b"subject:")) == 1
    assert not any(ln.lower().startswith(b"bcc:") for ln in lines)


def test_header_value_strips_every_control_and_caps_the_length() -> None:
    assert m.header_value("a\r\nb\tc\x00d") == "a b c d"
    assert len(m.header_value("x" * 1000)) == m.MAX_HEADER_CHARS
    assert m.header_value("  many   spaces  ") == "many spaces"


def test_the_message_id_is_deterministic_and_per_recipient() -> None:
    a = m.message_id(TID, 1, REF, "vektra.example.org")
    assert a == m.message_id(TID, 1, REF, "vektra.example.org")
    assert a != m.message_id(TID, 2, REF, "vektra.example.org")
    assert a != m.message_id(TID, 1, "b" * 64, "vektra.example.org")
    digest = hashlib.sha256(f"{TID}|1|{REF}".encode()).hexdigest()[:32]
    assert a == f"<{digest}@vektra.example.org>"
    # a domain cannot smuggle a header either
    assert "\n" not in m.message_id(TID, 1, REF, "x\r\nBcc: y")


def test_the_attachment_is_the_exact_bytes_as_utf8_html() -> None:
    parsed = email.message_from_bytes(_wire(_mail()), policy=policy.SMTP)
    parts = list(parsed.iter_attachments())
    assert len(parts) == 1
    part = parts[0]
    assert part.get_content_type() == "text/html"
    assert part.get_content_charset() == "utf-8"
    assert part.get_filename() == "EAASSESS_501_101_s2_20261107.html"
    assert part.get_payload(decode=True) == DOC


def test_the_body_is_plain_italian_and_content_free() -> None:
    parsed = email.message_from_bytes(_wire(_mail()), policy=policy.SMTP)
    plain = parsed.get_body(preferencelist=("plain",))
    assert plain is not None
    text = plain.get_content()
    assert "trascrizione" in text and "501" in text and "101" in text
    assert "Citt\u00e0" not in text and "<html" not in text  # nothing of the transcript
    subject = str(parsed["Subject"])
    assert "EAASSESS" in subject and "501" in subject and "101" in subject
    assert parsed["Message-ID"] == m.message_id(TID, 1, REF, "vektra.example.org")
    assert parsed["To"] == "prof@unical.it"
