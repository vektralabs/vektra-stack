"""``teacher_notify`` and ``operator_notify`` (worker jobs): the notification emails (REQ-071,
REQ-086; product decision 2026-11-08: by email through the transcript relay).

Producers (unchanged, all in the transaction of the event):

- ``assess_close_session`` v3, on every ``service_failure`` close: ``teacher_notify`` with
  ``{session_id, reason: service_failure}``;
- ``start_guard.after_rollback``, when a student's system-rollback streak reaches the threshold:
  ``teacher_notify`` and ``operator_notify`` with ``{site_id, cmid, student_userid, reason:
  rollback_cap}``;
- ``start_guard.alert_moodle_unreachable``: ``operator_notify`` with ``{site_id, reason, kind:
  moodle_unreachable}`` (one per site per 15 minutes).

Recipients: the teacher side is the activity's configured transcript recipients (the sealed
``transcript_recipients`` field, re-read on every run, the same domain allowlist); the operator
side is ``VEKTRA_ASSESS_OPERATOR_EMAIL`` (deployment configuration: no allowlist).

Delivery is the transcript's, row for row (``assess_deliveries`` rows of the notification's
``kind``, keyed by the first job's id, revision 0039): the attempt start is committed before
connecting, the outcome after, fenced on that start; a run that finds an attempt with no outcome
(a crash between the relay's 250 and the commit) resends it with the SAME Message-ID and audits
``assess_notify_possible_duplicate`` (OQ-029). Every attempt and every final result is audited.

Retries: one schedule per notification, the relay's (``VEKTRA_ASSESS_SMTP_RETRY_SCHEDULE``,
window ``VEKTRA_ASSESS_SMTP_RETRY_WINDOW_HOURS`` from the first run). A run that leaves a row to
retry (a retryable relay class, no relay session in time, recipients unreadable) completes and
queues the next run of the chain at the scheduled time; past the window the rows are FAILED.

Content-free: the body is a fixed Italian text with ids and fixed codes only (the activity, the
student's numeric LMS id and the session number, as in the transcript email; the operator email
carries no student id). Nothing here logs or audits an address: refs, ids, classes and codes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.delivery import message as mail
from vektra_assess.delivery.backoff import next_not_before
from vektra_assess.delivery.handler import (
    DOMAIN_NOT_ALLOWED,
    RECIPIENTS_UNRESOLVED,
    WINDOW_EXPIRED,
    AcquireDb,
    Sender,
)
from vektra_assess.delivery.recipients import (
    MAX_RECIPIENTS,
    ConfigRecipients,
    domain_allowed,
    recipient_ref,
)
from vektra_assess.delivery.smtp import ALERTING, SendResult, SlotBusy
from vektra_assess.jobs import JobKind, enqueue_job

log = structlog.get_logger(__name__)

TEACHER = JobKind.TEACHER_NOTIFY
OPERATOR = JobKind.OPERATOR_NOTIFY
KINDS = (TEACHER, OPERATOR)

SERVICE_FAILURE = "service_failure"
ROLLBACK_CAP = "rollback_cap"
LMS_UNREADABLE = "moodle_unreachable"
NO_RECIPIENTS = "no_recipients"
RECIPIENT_WITHDRAWN = "recipient_withdrawn"
#: a busy relay is tried again after this long (no attempt was made, nothing to back off from)
BUSY_RETRY = timedelta(seconds=30)

_CODE = re.compile(r"^[a-z_]{1,40}$")
LIVE = ("PENDING", "RETRYING")


@dataclass(frozen=True)
class NotifyDeps:
    recipients: ConfigRecipients
    sender: Sender
    sender_address: str
    message_id_domain: str
    domains: frozenset[str]
    #: ``VEKTRA_ASSESS_OPERATOR_EMAIL``; the operator handler is bound only with it set
    operator_address: str | None
    schedule: Sequence[timedelta]
    window: timedelta
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Event:
    """What happened, as ids and fixed codes. ``reason`` is the rollback reason code of an
    operator alert (a fixed word), never text."""

    code: str
    site_id: UUID
    cmid: int | None = None
    student_userid: int | None = None
    session_id: UUID | None = None
    session_no: int | None = None
    reason: str | None = None


@dataclass
class Notified:
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    duplicates: int = 0
    busy: int = 0
    unresolved: bool = False
    next_run: datetime | None = None


def _alert(event: str, kind: JobKind, **fields: Any) -> None:
    # the delivery alert event: plan 12's metrics and the operator read one stream
    log.error("assess.delivery.alert", alert=event, kind=kind.value, **fields)


# --- the event -------------------------------------------------------------------------------

_SESSION = text(
    "SELECT site_id, cmid, student_userid, session_no FROM assess_sessions WHERE id = :s"
)


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text_ = str(value)
    if not text_.isascii() or not text_.isdigit() or len(text_) > 18:
        return None
    return int(text_)


async def read_event(conn: Any, kind: JobKind, args: Mapping[str, Any]) -> Event | None:
    """The event a job describes, re-read where it can be. ``None``: malformed or stale (the
    session is gone), the job is a no-op that completes."""
    reason = args.get("reason")
    if "session_id" in args:
        sid = _uuid(args["session_id"])
        if sid is None or reason != SERVICE_FAILURE or kind is not TEACHER:
            return None
        row = (await conn.execute(_SESSION, {"s": sid})).first()
        if row is None:
            return None  # purged since: there is nothing left to point the teacher to
        return Event(
            SERVICE_FAILURE,
            row.site_id,
            cmid=int(row.cmid),
            student_userid=int(row.student_userid),
            session_id=sid,
            session_no=int(row.session_no),
        )
    site = _uuid(args.get("site_id"))
    if site is None:
        return None
    if args.get("kind") == LMS_UNREADABLE:
        if (
            kind is not OPERATOR
            or not isinstance(reason, str)
            or not _CODE.match(reason)
        ):
            return None
        return Event(LMS_UNREADABLE, site, reason=reason)
    if reason == ROLLBACK_CAP:
        cmid, userid = _int(args.get("cmid")), _int(args.get("student_userid"))
        if cmid is None or userid is None:
            return None
        return Event(ROLLBACK_CAP, site, cmid=cmid, student_userid=userid)
    return None


# --- the email -------------------------------------------------------------------------------

_FOOTER = "\nMessaggio automatico di Vektra: non rispondere a questo indirizzo.\n"


def _text(kind: JobKind, e: Event) -> tuple[str, str]:
    """Subject and body: fixed Italian sentences with ids and codes only."""
    if kind is TEACHER and e.code == SERVICE_FAILURE:
        return (
            f"Discussione chiusa per un guasto del servizio - attivita {e.cmid} - "
            f"studente {e.student_userid} - sessione #{e.session_no}",
            "Buongiorno,\n\n"
            "la discussione di valutazione indicata sotto e' stata chiusa per un guasto del "
            "servizio: il modello non ha risposto.\n\n"
            f"  attivita': {e.cmid}\n"
            f"  studente (id Moodle): {e.student_userid}\n"
            f"  sessione: #{e.session_no}\n\n"
            "Dall'elenco delle sessioni dell'attivita' potete concedere allo studente un nuovo "
            "tentativo.\n" + _FOOTER,
        )
    if kind is TEACHER and e.code == ROLLBACK_CAP:
        return (
            f"Avvio della discussione non riuscito piu volte - attivita {e.cmid} - "
            f"studente {e.student_userid}",
            "Buongiorno,\n\n"
            "lo studente indicato sotto non e' riuscito ad avviare la discussione di "
            "valutazione piu' volte di seguito, per un problema tecnico.\n\n"
            f"  attivita': {e.cmid}\n"
            f"  studente (id Moodle): {e.student_userid}\n\n"
            "L'amministratore della piattaforma ha ricevuto lo stesso avviso.\n"
            + _FOOTER,
        )
    if kind is OPERATOR and e.code == ROLLBACK_CAP:
        return (
            f"Vektra: avvii annullati ripetutamente - sito {e.site_id} - attivita {e.cmid}",
            "Piu' avvii consecutivi della stessa discussione sono stati annullati per un "
            "problema tecnico (REQ-071).\n\n"
            f"  sito: {e.site_id}\n"
            f"  attivita': {e.cmid}\n\n"
            "Controllare GET /health/assess e il registro del worker.\n" + _FOOTER,
        )
    if kind is OPERATOR and e.code == LMS_UNREADABLE:
        return (
            f"Vektra: Moodle non leggibile - sito {e.site_id}",
            "Il controllo prima dell'avvio di una discussione non ha potuto leggere Moodle.\n\n"
            f"  sito: {e.site_id}\n"
            f"  motivo: {e.reason}\n\n"
            "Controllare che Moodle sia raggiungibile e che il token del web service sia "
            "valido. L'avviso arriva al massimo una volta ogni 15 minuti per sito.\n"
            + _FOOTER,
        )
    raise ValueError(f"no text for {kind.value}/{e.code}")


def build(
    kind: JobKind,
    e: Event,
    *,
    sender: str,
    recipient: str,
    message_id: str,
    date: datetime,
) -> EmailMessage:
    subject, body = _text(kind, e)
    msg = EmailMessage(policy=SMTP)
    msg["From"] = mail.header_value(sender, limit=None)
    msg["To"] = mail.header_value(recipient, limit=None)
    msg["Subject"] = mail.header_value(subject)
    msg["Message-ID"] = mail.header_value(message_id, limit=None)
    msg["Date"] = format_datetime(date)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body, subtype="plain", charset="utf-8")
    return msg


def notify_ref(kind: JobKind, site_id: object, address: str) -> str:
    """The teacher's ref is the transcript rows' (``sha256(site|address)``); the operator's is
    not bound to a site."""
    return recipient_ref(site_id if kind is TEACHER else "operator", address)


# --- the rows ---------------------------------------------------------------------------------

_ROWS = text(
    "SELECT id, recipient_ref, status, attempt_started_at FROM assess_deliveries "
    "WHERE job_id = :j AND kind = :k ORDER BY created_at, id"
)
_ADD = text(
    "INSERT INTO assess_deliveries (kind, job_id, recipient_kind, recipient_ref, status, "
    "not_before) VALUES (:k, :j, 'address', :ref, 'PENDING', :now) ON CONFLICT DO NOTHING"
)
_WITHDRAW = text(
    "UPDATE assess_deliveries SET status = 'FAILED', last_error_class = :cls, "
    "attempt_started_at = NULL WHERE id = :i AND status IN ('PENDING', 'RETRYING') "
    "RETURNING id, recipient_ref, attempt_started_at"
)
_CLAIM = text(
    "WITH due AS (SELECT id, attempt_started_at AS prev FROM assess_deliveries "
    "WHERE id = :i AND status IN ('PENDING', 'RETRYING') FOR UPDATE SKIP LOCKED) "
    "UPDATE assess_deliveries d SET attempt_started_at = :now, attempts = d.attempts + 1, "
    "message_id = COALESCE(d.message_id, :mid) FROM due WHERE d.id = due.id "
    "RETURNING d.id, d.recipient_ref, d.attempts, due.prev, d.message_id"
)
_SENT = text(
    "UPDATE assess_deliveries SET status = 'SENT', sent_at = :now, smtp_code = :code, "
    "last_error_class = NULL, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)
_RETRY = text(
    "UPDATE assess_deliveries SET status = 'RETRYING', last_error_class = :cls, "
    "smtp_code = :code, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)
_FAIL = text(
    "UPDATE assess_deliveries SET status = 'FAILED', last_error_class = :cls, "
    "smtp_code = :code, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)
_LIVE_ROWS = text(
    "UPDATE assess_deliveries SET not_before = :nb WHERE job_id = :j AND kind = :k "
    "AND status IN ('PENDING', 'RETRYING')"
)


@dataclass(frozen=True)
class _Run:
    """One run of a notification chain."""

    kind: JobKind
    root: int  # the first job's id: the rows' key and the Message-ID's
    try_no: int  # 1 on the first run of the chain
    since: datetime  # the first run's start: the retry window counts from here
    event: Event
    job_id: int
    args: Mapping[str, Any]


@dataclass(frozen=True)
class _Claimed:
    id: UUID
    ref: str
    attempts: int
    started: datetime
    duplicate: bool
    message_id: str


async def _audit(
    conn: Any,
    event: AssessAuditEvent,
    run: _Run,
    *,
    reason: str | None = None,
    **data: Any,
) -> None:
    await write_event(
        conn,
        event,
        actor="worker",
        site_id=run.event.site_id,
        session_id=run.event.session_id,
        reason_code=reason,
        data={"kind": run.kind.value, "event": run.event.code, "job": run.root, **data},
    )


async def _claim(deps: NotifyDeps, acquire: AcquireDb, run: _Run, row_id: UUID, ref: str,
                 out: Notified) -> _Claimed | None:  # fmt: skip
    now = deps.clock()
    mid = mail.message_id(
        f"notify-{run.kind.value}-{run.root}", 1, ref, deps.message_id_domain
    )
    async with acquire() as conn, conn.begin():
        c = (await conn.execute(_CLAIM, {"i": row_id, "now": now, "mid": mid})).first()
        if c is None:
            return None
        if c.prev is not None:
            # an attempt started and left no outcome: maybe the relay holds it already
            await _audit(
                conn,
                AssessAuditEvent.NOTIFY_POSSIBLE_DUPLICATE,
                run,
                ref=c.recipient_ref,
                delivery_id=str(c.id),
            )
            out.duplicates += 1
    return _Claimed(
        c.id,
        c.recipient_ref,
        int(c.attempts),
        now,
        c.prev is not None,
        str(c.message_id),
    )


async def _settle(
    deps: NotifyDeps,
    acquire: AcquireDb,
    run: _Run,
    c: _Claimed,
    result: SendResult,
    reason: str | None,
    out: Notified,
    *,
    final: bool = False,
) -> bool:
    """Commit one attempt's outcome, fenced on its start. True when the row is to be retried."""
    cls = reason or (result.error_class.value if result.error_class else None)
    base = {"i": c.id, "started": c.started, "code": result.smtp_code}
    if result.ok:
        stmt, params, outcome = _SENT, {**base, "now": deps.clock()}, "sent"
    elif final or (result.error_class is not None and not result.retryable):
        stmt, params, outcome = _FAIL, {**base, "cls": cls}, "failed"
    else:
        stmt, params, outcome = _RETRY, {**base, "cls": cls}, "retrying"
    ids = {"ref": c.ref, "delivery_id": str(c.id)}
    async with acquire() as conn, conn.begin():
        if not (await conn.execute(stmt, params)).rowcount:
            log.warning(
                "assess_notify_fence_lost", kind=run.kind.value, job_id=run.root
            )
            return False
        await _audit(
            conn,
            AssessAuditEvent.NOTIFY_ATTEMPT,
            run,
            reason=cls,
            attempt=c.attempts,
            outcome=outcome,
            smtp_code=result.smtp_code,
            duplicate=c.duplicate,
            **ids,
        )
        if outcome == "sent":
            await _audit(conn, AssessAuditEvent.NOTIFY_SENT, run, **ids)
        elif outcome == "failed":
            await _audit(conn, AssessAuditEvent.NOTIFY_FAILED, run, reason=cls, **ids)
    if outcome == "sent":
        out.sent += 1
    elif outcome == "failed":
        out.failed += 1
        _alert("notify_failed", run.kind, reason_code=cls)
    else:
        out.retrying += 1
    if result.error_class in ALERTING:
        _alert(f"smtp_{result.error_class.value}", run.kind)
    return outcome == "retrying"


async def _row(
    deps: NotifyDeps,
    acquire: AcquireDb,
    run: _Run,
    row_id: UUID,
    ref: str,
    address: str,
    out: Notified,
) -> bool:
    """One recipient. True when it is to be retried by the next run."""
    if run.kind is TEACHER and not domain_allowed(address, deps.domains):
        c = await _claim(deps, acquire, run, row_id, ref, out)
        if c is None:
            return False
        await _settle(
            deps,
            acquire,
            run,
            c,
            SendResult(False),
            DOMAIN_NOT_ALLOWED,
            out,
            final=True,
        )
        return False
    try:
        async with deps.sender.slot():
            c = await _claim(deps, acquire, run, row_id, ref, out)
            if c is None:
                return False
            msg = build(
                run.kind,
                run.event,
                sender=deps.sender_address,
                recipient=address,
                message_id=c.message_id,
                date=deps.clock(),
            )
            result = await deps.sender.send(msg, address, slot_held=True)
            del msg
            return await _settle(deps, acquire, run, c, result, None, out)
    except SlotBusy:
        out.busy += 1  # no relay session in time: the row stays live, untouched
        return True


async def _recipients(
    deps: NotifyDeps, acquire: AcquireDb, run: _Run
) -> tuple[str, ...] | None:
    """The addresses now: ``None`` unreadable, ``()`` nobody configured."""
    if run.kind is OPERATOR:
        if deps.operator_address is None:
            return None  # not reached: the handler is bound only with the address set
        return (deps.operator_address,)
    if run.event.cmid is None:
        return ()
    async with acquire() as conn:
        found = await deps.recipients.read(conn, run.event.site_id, run.event.cmid)
        await conn.rollback()
    return found


async def _reconcile(
    conn: Any,
    deps: NotifyDeps,
    run: _Run,
    by_ref: Mapping[str, str] | None,
    out: Notified,
) -> list[Any]:
    """Rows for the recipients configured now; a live row whose recipient is no longer
    configured is FAILED (``recipient_withdrawn``) first, so it frees its slot: never more than
    ``MAX_RECIPIENTS`` rows live or SENT. The live rows left."""
    now = deps.clock()
    rows = (await conn.execute(_ROWS, {"j": run.root, "k": run.kind.value})).all()
    if by_ref is not None:
        for r in rows:
            if r.status in LIVE and r.recipient_ref not in by_ref:
                gone = (
                    await conn.execute(
                        _WITHDRAW, {"i": r.id, "cls": RECIPIENT_WITHDRAWN}
                    )
                ).first()
                if gone is not None:
                    await _audit(
                        conn,
                        AssessAuditEvent.NOTIFY_FAILED,
                        run,
                        reason=RECIPIENT_WITHDRAWN,
                        ref=r.recipient_ref,
                        delivery_id=str(r.id),
                    )
                    out.failed += 1
        rows = (await conn.execute(_ROWS, {"j": run.root, "k": run.kind.value})).all()
        have = {r.recipient_ref for r in rows}
        room = MAX_RECIPIENTS - sum(1 for r in rows if r.status in (*LIVE, "SENT"))
        for ref in by_ref:
            if ref not in have and room > 0:
                await conn.execute(
                    _ADD, {"k": run.kind.value, "j": run.root, "ref": ref, "now": now}
                )
                room -= 1
        rows = (await conn.execute(_ROWS, {"j": run.root, "k": run.kind.value})).all()
    return [r for r in rows if r.status in LIVE]


async def _expire(
    deps: NotifyDeps, acquire: AcquireDb, run: _Run, out: Notified
) -> None:
    """The window is over: every live row is FAILED (``window_expired``), audited."""
    async with acquire() as conn, conn.begin():
        rows = (await conn.execute(_ROWS, {"j": run.root, "k": run.kind.value})).all()
        for r in rows:
            if r.status not in LIVE:
                continue
            gone = (
                await conn.execute(_WITHDRAW, {"i": r.id, "cls": WINDOW_EXPIRED})
            ).first()
            if gone is None:
                continue
            extra: dict[str, Any] = {}
            if gone.attempt_started_at is not None:
                extra["last_attempt_outcome"] = "unknown"  # the relay may hold it
            await _audit(
                conn,
                AssessAuditEvent.NOTIFY_FAILED,
                run,
                reason=WINDOW_EXPIRED,
                ref=r.recipient_ref,
                delivery_id=str(r.id),
                **extra,
            )
            out.failed += 1
            _alert("notify_failed", run.kind, reason_code=WINDOW_EXPIRED)
        if out.unresolved and not rows:
            # nothing was ever addressed: the notification itself failed
            await _audit(
                conn, AssessAuditEvent.NOTIFY_FAILED, run, reason=RECIPIENTS_UNRESOLVED
            )
            out.failed += 1
            _alert("notify_failed", run.kind, reason_code=RECIPIENTS_UNRESOLVED)


async def _next_run(
    deps: NotifyDeps, acquire: AcquireDb, run: _Run, out: Notified, busy_only: bool
) -> None:
    """Queue the chain's next run at the scheduled time, or end it past the window."""
    now = deps.clock()
    nb: datetime | None
    if busy_only:
        soon = min(now + BUSY_RETRY, run.since + deps.window)
        nb = soon if soon > now else None
    else:
        nb = next_not_before(run.try_no, run.since, now, deps.schedule, deps.window)
    if nb is None:
        await _expire(deps, acquire, run, out)
        return
    args = {
        **{
            k: v
            for k, v in run.args.items()
            if k not in ("root_job_id", "try", "since")
        },
        "root_job_id": run.root,
        "try": run.try_no + 1,
        "since": int(run.since.timestamp()),
    }
    async with acquire() as conn, conn.begin():
        await conn.execute(_LIVE_ROWS, {"nb": nb, "j": run.root, "k": run.kind.value})
        await enqueue_job(
            conn,
            run.kind,
            args,
            # one key per parent job: the next run is queued once, even when this run is retried
            dedupe_key=f"{run.kind.value}:next:{run.root}:{run.job_id}",
            not_before=nb,
        )
    out.next_run = nb


def _run(kind: JobKind, ctx: Any, event: Event, now: datetime) -> _Run:
    args = ctx.args
    root = _int(args.get("root_job_id")) or int(ctx.job_id)
    try_no = _int(args.get("try")) or 1
    since_s = _int(args.get("since"))
    since = datetime.fromtimestamp(since_s, UTC) if since_s is not None else now
    return _Run(kind, root, try_no, since, event, int(ctx.job_id), args)


async def notify(deps: NotifyDeps, kind: JobKind, ctx: Any) -> Notified | None:
    """One run of a notification. ``None``: malformed or stale job (a no-op that completes)."""
    now = deps.clock()
    async with ctx.acquire() as conn:
        event = await read_event(conn, kind, ctx.args)
        await conn.rollback()
    if event is None:
        log.info("assess_notify_stale", kind=kind.value, job_id=int(ctx.job_id))
        return None
    run = _run(kind, ctx, event, now)
    out = Notified()
    configured = await _recipients(deps, ctx.acquire, run)
    by_ref = (
        None
        if configured is None
        else {notify_ref(kind, event.site_id, a): a for a in configured}
    )
    async with ctx.acquire() as conn, conn.begin():
        live = await _reconcile(conn, deps, run, by_ref, out)
        if by_ref is not None and not by_ref and not live:
            rows = (await conn.execute(_ROWS, {"j": run.root, "k": kind.value})).all()
            if not rows:
                # readable and empty: the teacher configured nobody to tell
                await _audit(
                    conn, AssessAuditEvent.NOTIFY_FAILED, run, reason=NO_RECIPIENTS
                )
                out.failed += 1
                _alert("notify_failed", kind, reason_code=NO_RECIPIENTS)
    if by_ref is None:
        out.unresolved = True
        _alert(RECIPIENTS_UNRESOLVED, kind)
        await _next_run(deps, ctx.acquire, run, out, busy_only=False)
        return out
    retry = await asyncio.gather(
        *(
            _row(
                deps,
                ctx.acquire,
                run,
                r.id,
                r.recipient_ref,
                by_ref[r.recipient_ref],
                out,
            )
            for r in live
        )
    )
    if any(retry):
        await _next_run(deps, ctx.acquire, run, out, busy_only=out.retrying == 0)
    log.info(
        "assess_notify_run",
        kind=kind.value,
        job_id=run.root,
        outcome=(
            f"sent={out.sent},retrying={out.retrying},failed={out.failed},busy={out.busy}"
        ),
    )
    return out


def make_notify_handler(
    deps: NotifyDeps, kind: JobKind
) -> Callable[[Any], Awaitable[None]]:
    if kind not in KINDS:
        raise ValueError(f"not a notification kind: {kind}")

    async def handle(ctx: Any) -> None:
        await notify(deps, kind, ctx)

    return handle
