"""The operator's SMTP test send (plan assess-privacy-retention 12b, REQ-080).

``POST /api/v1/assess/ops/smtp-test`` queues an ``smtp_test_send`` job with a test id the API
generated; this handler sends ONE fixed, content-free message to the configured operator address
(``VEKTRA_ASSESS_OPERATOR_EMAIL``) and records the outcome in an ``assess_deliveries`` row of kind
``smtp_test`` whose id is that test id, so ``GET .../smtp-test/{id}`` can read it back over the
columns the API may read (status, error class, SMTP code). The recipient never comes from the
request.

One attempt only (the job's ``max_attempts`` is 1): a test reports what the relay did the first
time. A rerun of the same job (the lease expired after a crash) never sends a second copy: a row
that was claimed and has no outcome becomes FAILED ``interrupted``. The purge tick deletes these
rows after ``RETENTION``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.delivery import message as mail
from vektra_assess.delivery.recipients import recipient_ref
from vektra_assess.delivery.smtp import SendResult, SlotBusy

log = structlog.get_logger(__name__)

KIND = "smtp_test"
RETENTION = timedelta(days=7)
INTERRUPTED = "interrupted"
BUSY = "relay_busy"
SUBJECT = "Vektra: prova di invio SMTP"
BODY = (
    "Questo e' un messaggio di prova inviato dall'operatore della piattaforma Vektra per "
    "verificare il relay SMTP. Non contiene dati di studenti o di sessioni.\n"
    "\n"
    "Messaggio automatico di Vektra: non rispondere a questo indirizzo.\n"
)


class Sender(Protocol):
    def send(self, msg: EmailMessage, recipient: str) -> Awaitable[SendResult]: ...


@dataclass(frozen=True)
class SmtpTestDeps:
    sender: Sender
    sender_address: str
    message_id_domain: str
    operator_address: str
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


_ADD = text(
    "INSERT INTO assess_deliveries (id, kind, recipient_kind, recipient_ref, status, "
    "not_before, created_at) VALUES (:i, 'smtp_test', 'address', :ref, 'PENDING', :now, :now) "
    "ON CONFLICT (id) DO NOTHING"
)
#: the claim: PENDING and never attempted -> attempted now; RETURNING nothing means the row was
#: already attempted (a rerun) or settled
_CLAIM = text(
    "UPDATE assess_deliveries SET attempt_started_at = :now, attempts = attempts + 1 "
    "WHERE id = :i AND kind = 'smtp_test' AND status = 'PENDING' "
    "AND attempt_started_at IS NULL RETURNING id"
)
_INTERRUPT = text(
    "UPDATE assess_deliveries SET status = 'FAILED', last_error_class = :cls, "
    "attempt_started_at = NULL WHERE id = :i AND kind = 'smtp_test' AND status = 'PENDING'"
)
_SENT = text(
    "UPDATE assess_deliveries SET status = 'SENT', sent_at = :now, smtp_code = :code, "
    "attempt_started_at = NULL WHERE id = :i AND kind = 'smtp_test'"
)
_FAILED = text(
    "UPDATE assess_deliveries SET status = 'FAILED', last_error_class = :cls, "
    "smtp_code = :code, attempt_started_at = NULL WHERE id = :i AND kind = 'smtp_test'"
)


def build(deps: SmtpTestDeps, test_id: UUID, ref: str, now: datetime) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = mail.header_value(deps.sender_address, limit=None)
    msg["To"] = mail.header_value(deps.operator_address, limit=None)
    msg["Subject"] = mail.header_value(SUBJECT)
    msg["Message-ID"] = mail.header_value(
        mail.message_id(test_id, 1, ref, deps.message_id_domain), limit=None
    )
    msg["Date"] = format_datetime(now)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(BODY, subtype="plain", charset="utf-8")
    return msg


def _test_id(args: Any) -> UUID | None:
    raw = args.get("test_id") if hasattr(args, "get") else None
    try:
        return UUID(str(raw)) if raw is not None else None
    except ValueError:
        return None


async def run_test(deps: SmtpTestDeps, ctx: Any) -> str:
    """One test send. Returns the outcome code (``sent``, an error class, or a fixed code)."""
    test_id = _test_id(ctx.args)
    if test_id is None:
        log.warning("assess_smtp_test_malformed", job_id=int(ctx.job_id))
        return "malformed"
    ref = recipient_ref("ops-test", deps.operator_address)
    now = deps.clock()
    async with ctx.acquire() as conn, conn.begin():
        await conn.execute(_ADD, {"i": test_id, "ref": ref, "now": now})
        claimed = (await conn.execute(_CLAIM, {"i": test_id, "now": now})).first()
        if claimed is None:
            # a rerun after a crash: the first run may have reached the relay; never send twice
            res = await conn.execute(_INTERRUPT, {"i": test_id, "cls": INTERRUPTED})
            if res.rowcount:
                await _audit(conn, test_id, INTERRUPTED)
            return INTERRUPTED
    try:
        result = await deps.sender.send(
            build(deps, test_id, ref, now), deps.operator_address
        )
    except SlotBusy:
        result = SendResult(False, None, None)
        outcome = BUSY
    else:
        outcome = (
            "sent"
            if result.ok
            else (result.error_class.value if result.error_class else "connect")
        )
    async with ctx.acquire() as conn, conn.begin():
        if result.ok:
            await conn.execute(
                _SENT, {"i": test_id, "now": deps.clock(), "code": result.smtp_code}
            )
        else:
            await conn.execute(
                _FAILED, {"i": test_id, "cls": outcome, "code": result.smtp_code}
            )
        await _audit(conn, test_id, outcome)
    log.info("assess_smtp_test_done", reason_code=outcome, job_id=int(ctx.job_id))
    return outcome


async def _audit(conn: Any, test_id: UUID, outcome: str) -> None:
    await write_event(
        conn,
        AssessAuditEvent.SMTP_TEST_RESULT,
        actor="worker",
        reason_code=outcome,
        data={"test_id": str(test_id)},
    )


def make_smtp_test_handler(deps: SmtpTestDeps) -> Callable[[Any], Awaitable[None]]:
    async def handle(ctx: Any) -> None:
        await run_test(deps, ctx)

    return handle
