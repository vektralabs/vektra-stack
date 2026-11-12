"""The transcript email (REQ-075, COMP-007): headers, a content-free body, the file attached.

- ``email.message.EmailMessage`` with ``policy=SMTP`` (CRLF line ends, RFC 5322 folding).
- Every header-bound value goes through ``header_value`` first: CR, LF and every other C0/C1
  control character are removed, runs of whitespace collapse, the length is capped. A course
  short name carrying ``"\\r\\nBcc: x"`` stays one Subject header on the wire.
- The plain-text body is a fixed Italian sentence with ids only: no transcript content, no
  name, no grade (the transcript itself is the attachment).
- The attachment is the transcript's exact stored bytes, ``text/html; charset=utf-8``, under the
  BR-016 file name (identifiers only, OQ-020), so the email copy hash-equals the download copy.
- ``Message-ID: <sha256(tid|round|recipient_ref)[:32]@domain>`` is deterministic: a resend after a
  crash in the window between the relay's 250 and the SENT commit carries the SAME id (OQ-029),
  so a duplicate is recognisable as one.

Nothing here logs; the module never sees anything but the one address it addresses.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

#: C0 controls, DEL and C1 controls (U+0080..U+009F), plus the Unicode line/paragraph separators
_CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029]")
_SPACES = re.compile(r"\s+")
MAX_HEADER_CHARS = 200

#: the attachment's MIME type (BR-016: the same type the download serves)
ATTACHMENT_TYPE = ("text", "html")

BODY_TEMPLATE = (
    "Buongiorno,\n"
    "\n"
    "in allegato la trascrizione della discussione di valutazione:\n"
    "\n"
    "  corso: {course}\n"
    "  attivita': {cmid}\n"
    "  studente (id LMS): {student}\n"
    "  sessione: #{session_no}\n"
    "\n"
    "Il file e' in formato HTML: apritelo con un browser. Lo stesso file si scarica dalla "
    "pagina dell'attivita' nell'LMS.\n"
    "\n"
    "Messaggio automatico di Vektra: non rispondere a questo indirizzo.\n"
)


def header_value(value: object, *, limit: int | None = MAX_HEADER_CHARS) -> str:
    """A value safe to put in a header: no control character survives (CR/LF injection),
    whitespace collapsed, capped at ``limit`` characters (``None``: never cut, for the
    structured values that must equal the envelope or the stored row)."""
    text = _SPACES.sub(" ", _CONTROL.sub(" ", str(value))).strip()
    return text if limit is None else text[:limit]


def message_id(
    transcript_id: object, round_: int, recipient_ref: str, domain: str
) -> str:
    """Deterministic ``Message-ID`` of one (transcript, round, recipient): the same on every
    attempt, so a crash-window duplicate carries the id of the first copy (OQ-029)."""
    digest = hashlib.sha256(
        f"{transcript_id}|{int(round_)}|{recipient_ref}".encode()
    ).hexdigest()[:32]
    host = header_value(domain).replace(" ", "").strip("<>@") or "vektra.invalid"
    return f"<{digest}@{host}>"


@dataclass(frozen=True)
class TranscriptMail:
    """Everything one transcript email carries. ``course`` may be anything an LMS course
    short name can be (it is sanitised here, never trusted)."""

    sender: str
    recipient: str
    course: str
    cmid: int
    student_userid: int
    session_no: int
    filename: str
    attachment: bytes
    message_id: str
    date: datetime


def subject(mail: TranscriptMail) -> str:
    """REQ-075: course, activity and student in the subject (the student by LMS id until the
    DPO rules on full names, OQ-020)."""
    head = "Trascrizione discussione - "
    tail = (
        f" - attivita {int(mail.cmid)} - studente {int(mail.student_userid)} - "
        f"sessione #{int(mail.session_no)}"
    )
    # the course takes what is left: a long name never pushes the ids out
    room = max(MAX_HEADER_CHARS - len(head) - len(tail), 1)
    return header_value(head + header_value(mail.course, limit=room) + tail)


def body(mail: TranscriptMail) -> str:
    return BODY_TEMPLATE.format(
        course=header_value(mail.course),
        cmid=int(mail.cmid),
        student=int(mail.student_userid),
        session_no=int(mail.session_no),
    )


def build(mail: TranscriptMail) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    # addresses and the Message-ID are validated values: sanitised, never cut, so the
    # headers equal the SMTP envelope and the stored Message-ID
    msg["From"] = header_value(mail.sender, limit=None)
    msg["To"] = header_value(mail.recipient, limit=None)
    msg["Subject"] = subject(mail)
    msg["Message-ID"] = header_value(mail.message_id, limit=None)
    msg["Date"] = format_datetime(mail.date)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body(mail), subtype="plain", charset="utf-8")
    msg.add_attachment(
        mail.attachment,
        maintype=ATTACHMENT_TYPE[0],
        subtype=ATTACHMENT_TYPE[1],
        filename=header_value(mail.filename, limit=120),
        params={"charset": "utf-8"},
    )
    return msg
