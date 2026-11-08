"""``transcript_deliver`` (worker job) and the ``delivery_retry`` tick (REQ-075, REQ-082, OQ-029).

One job delivers the due rows of ONE transcript (args ``transcript_id``):

1. **Recipients, re-read now** (REQ-082 as amended). The activity's sealed teacher config is
   opened again; under the per-transcript lock (the one ``assess_enqueue_resend`` takes), for
   every round that still has live rows (or round 1 of a transcript that has none):
   a live row whose ref is no longer configured -> ``REPLACED`` (audited, old ref); a configured
   ref with no row in the round -> a new ``PENDING`` row (audited, new ref), never past 3 rows
   that are not ``REPLACED``. Config unreadable or empty: the rows stay, the due ones go
   ``RETRYING`` with ``recipients_unresolved`` (backoff, alert); the transcript stays downloadable.
2. **Attempt start, committed before connecting** (OQ-029): the due rows (``PENDING`` /
   ``RETRYING``, ``not_before <= now``, not in flight) get ``attempt_started_at = now`` and
   ``attempts + 1`` in one statement (``FOR UPDATE SKIP LOCKED``: two jobs never take one row).
   A row whose previous attempt started but never recorded an outcome (older than the job lease:
   a crash between the relay's 250 and the SENT commit, or before) is taken again and an
   ``assess_delivery_possible_duplicate`` event is audited: it is resent with the SAME Message-ID.
3. **Send**: the transcript is decrypted and re-hashed once; per row, the address is the one the
   config maps to the row's ref; an address outside ``VEKTRA_ASSESS_RECIPIENT_DOMAINS`` (or any
   address with the list empty) is ``FAILED`` with ``domain_not_allowed`` without a connection.
4. **Outcome, one short transaction per row**, fenced on the attempt's own start time:
   relay 250 -> ``SENT`` (``sent_at``, Message-ID); a synchronous 5xx -> ``FAILED``; a retryable
   class -> ``RETRYING`` with the next ``not_before``, or ``FAILED`` once the retry window
   (from the row's creation) is over. Every attempt is audited (``assess_delivery_attempt``) and
   every final result too (``assess_delivery_sent`` / ``assess_delivery_failed``).

Alerts (ERROR ``assess.delivery.alert``, class only): every FAILED, every TLS or AUTH failure,
unresolved recipients. Nothing here logs or audits an address or a body: refs, ids, classes,
codes and counts only.

The ``delivery_retry`` tick enqueues a job for every transcript with due rows, and (throttled)
for a transcript that has no delivery row yet, so recipients configured after the close are
fanned out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.delivery import message as mail
from vektra_assess.delivery.backoff import next_not_before
from vektra_assess.delivery.recipients import (
    MAX_RECIPIENTS,
    ConfigRecipients,
    domain_allowed,
    recipient_ref,
)
from vektra_assess.delivery.smtp import ALERTING, SendResult, SlotBusy
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.keys import AssessKeyring
from vektra_assess.transcript import store

log = structlog.get_logger(__name__)

LIVE = ("PENDING", "RETRYING")
DOMAIN_NOT_ALLOWED = "domain_not_allowed"
RECIPIENTS_UNRESOLVED = "recipients_unresolved"
INTEGRITY = "transcript_integrity"
TRANSCRIPT_KEY = "transcript_key"


class Sender(Protocol):
    def slot(self) -> AbstractAsyncContextManager[None]: ...

    async def send(
        self, msg: Any, recipient: str, *, slot_held: bool = False
    ) -> SendResult: ...


AcquireDb = Callable[[], Any]  # an async context manager yielding an AsyncConnection


@dataclass(frozen=True)
class DeliveryDeps:
    transcript_kek: AssessKeyring
    recipients: ConfigRecipients
    sender: Sender
    sender_address: str
    message_id_domain: str
    domains: frozenset[str]
    schedule: Sequence[timedelta]
    window: timedelta
    #: an attempt started longer ago than this without an outcome is a crash in the window
    #: (at least the job lease, so a job still inside its budget is never raced)
    stale_after: timedelta
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass
class Delivered:
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    replaced: int = 0
    added: int = 0
    duplicates: int = 0
    busy: int = 0  # rows left due: no relay session within the bounded wait
    unresolved: bool = False


async def _all(*coros: Awaitable[None]) -> None:
    """Run every row's attempt to its end, even when one of them fails, then raise the first
    failure (the job is retried; rows left in flight are taken again after the lease)."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r


def _alert(event: str, **fields: Any) -> None:
    # ERROR level, class and ids only: plan 12's metrics and the operator read this event
    log.error("assess.delivery.alert", alert=event, **fields)


_TRANSCRIPT = text(
    "SELECT t.id, (t.ciphertext IS NOT NULL) AS live, t.filename, "
    "t.course_shortname, s.site_id, "
    "s.cmid, s.student_userid, s.session_no FROM assess_transcripts t "
    "JOIN assess_sessions s ON s.id = t.session_id WHERE t.id = :t"
)
_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('assess_resend:' || CAST(:t AS text), 0))"
)
_ROWS = text(
    "SELECT id, round, recipient_ref, status, attempt_started_at FROM assess_deliveries "
    "WHERE transcript_id = :t AND kind = 'transcript' ORDER BY round, created_at"
)
_REPLACE = text(
    "UPDATE assess_deliveries SET status = 'REPLACED', attempt_started_at = NULL "
    "WHERE id = :i AND status IN ('PENDING', 'RETRYING')"
)
_ADD = text(
    "INSERT INTO assess_deliveries (transcript_id, kind, round, recipient_kind, "
    "recipient_ref, status, not_before) VALUES (:t, 'transcript', :r, 'address', :ref, "
    "'PENDING', :now) ON CONFLICT (transcript_id, round, recipient_ref) DO NOTHING "
    "RETURNING id"
)
_READD = text(
    "UPDATE assess_deliveries SET status = 'PENDING', not_before = :now, "
    "last_error_class = NULL, smtp_code = NULL, attempt_started_at = NULL "
    "WHERE id = :i AND status = 'REPLACED' RETURNING id"
)
#: the bytes, read only after a claim returned a row (review #8)
_DOC = text(
    "SELECT ciphertext, dek_wrapped, content_hash FROM assess_transcripts "
    "WHERE id = :t AND ciphertext IS NOT NULL"
)
_DUE_IDS = text(
    "SELECT id, recipient_ref FROM assess_deliveries WHERE transcript_id = :t "
    "AND kind = 'transcript' AND status IN ('PENDING', 'RETRYING') AND not_before <= :now "
    "AND (attempt_started_at IS NULL OR attempt_started_at < :stale) "
    "ORDER BY round, created_at"
)
_CLAIM_ONE = text(
    "WITH due AS (SELECT id, attempt_started_at AS prev FROM assess_deliveries "
    "WHERE id = :i AND status IN ('PENDING', 'RETRYING') AND not_before <= :now "
    "AND (attempt_started_at IS NULL OR attempt_started_at < :stale) "
    "FOR UPDATE SKIP LOCKED) "
    "UPDATE assess_deliveries d SET attempt_started_at = :now, attempts = d.attempts + 1 "
    "FROM due WHERE d.id = due.id "
    "RETURNING d.id, d.round, d.recipient_ref, d.attempts, d.created_at, due.prev"
)
_CLAIM = text(
    "WITH due AS (SELECT id, attempt_started_at AS prev FROM assess_deliveries "
    "WHERE transcript_id = :t AND kind = 'transcript' AND status IN ('PENDING', 'RETRYING') "
    "AND not_before <= :now AND (attempt_started_at IS NULL OR attempt_started_at < :stale) "
    "FOR UPDATE SKIP LOCKED) "
    "UPDATE assess_deliveries d SET attempt_started_at = :now, attempts = d.attempts + 1 "
    "FROM due WHERE d.id = due.id "
    "RETURNING d.id, d.round, d.recipient_ref, d.attempts, d.created_at, due.prev"
)
_SENT = text(
    "UPDATE assess_deliveries SET status = 'SENT', sent_at = :now, smtp_code = :code, "
    "message_id = :mid, last_error_class = NULL, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)
_RETRY = text(
    "UPDATE assess_deliveries SET status = 'RETRYING', not_before = :nb, "
    "last_error_class = :cls, smtp_code = :code, message_id = :mid, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)
_FAIL = text(
    "UPDATE assess_deliveries SET status = 'FAILED', last_error_class = :cls, "
    "smtp_code = :code, message_id = :mid, attempt_started_at = NULL "
    "WHERE id = :i AND attempt_started_at = :started"
)


@dataclass(frozen=True)
class _Claimed:
    id: UUID
    round: int
    ref: str | None
    attempts: int
    created_at: datetime
    started: datetime
    duplicate: bool


async def _audit(
    conn: Any,
    event: AssessAuditEvent,
    t: Any,
    *,
    reason: str | None = None,
    **data: Any,
) -> None:
    await write_event(
        conn,
        event,
        actor="worker",
        site_id=t.site_id,
        session_id=t.id,  # the transcript id IS the session id
        reason_code=reason,
        data=data,
    )


async def _fan_out(
    conn: Any, t: Any, refs: Sequence[str], now: datetime, out: Delivered
) -> None:
    """Make the round's rows match the configuration NOW (REQ-082 as amended).

    The cap counts the refs configured now (``configured_recipients`` keeps at most 3), so
    every configured ref has exactly one row per round: missing -> INSERT, ``REPLACED`` ->
    back to ``PENDING`` (re-added). A live row whose ref is no longer configured is
    ``REPLACED``; a final row (``SENT``, ``FAILED``) of such a ref stays as history and takes
    no slot. A row in flight is left alone (its outcome decides; the next pass looks again).
    """
    assert len(refs) <= MAX_RECIPIENTS  # configured_recipients caps the configuration
    rows = (await conn.execute(_ROWS, {"t": t.id})).all()
    rounds = sorted({r.round for r in rows if r.status in LIVE}) or (
        [] if rows else [1]
    )
    for rnd in rounds:
        mine = [r for r in rows if r.round == rnd]
        for r in mine:
            in_flight = r.attempt_started_at is not None
            if r.status in LIVE and r.recipient_ref not in refs and not in_flight:
                await conn.execute(_REPLACE, {"i": r.id})
                await _audit(
                    conn,
                    AssessAuditEvent.DELIVERY_RECIPIENT_REPLACED,
                    t,
                    round=rnd,
                    old=r.recipient_ref,
                    delivery_id=str(r.id),
                )
                out.replaced += 1
        by_ref = {r.recipient_ref: r for r in mine if r.recipient_ref is not None}
        for ref in refs:
            row = by_ref.get(ref)
            reason: str | None = None
            if row is None:
                got = (
                    await conn.execute(
                        _ADD, {"t": t.id, "r": rnd, "ref": ref, "now": now}
                    )
                ).scalar()
            elif row.status == "REPLACED":
                got = (await conn.execute(_READD, {"i": row.id, "now": now})).scalar()
                reason = "re_added"
            else:
                continue  # already has its row (live, SENT or FAILED)
            if got is not None:
                await _audit(
                    conn,
                    AssessAuditEvent.DELIVERY_RECIPIENT_ADDED,
                    t,
                    reason=reason,
                    round=rnd,
                    new=ref,
                    delivery_id=str(got),
                )
                out.added += 1


async def _unresolved(
    conn: Any, deps: DeliveryDeps, t: Any, now: datetime, out: Delivered
) -> None:
    """No readable recipient: due rows wait (RETRYING, backoff) or end FAILED past the window."""
    out.unresolved = True
    stale = now - deps.stale_after
    claimed = (
        await conn.execute(_CLAIM, {"t": t.id, "now": now, "stale": stale})
    ).all()
    for c in claimed:
        nb = next_not_before(
            int(c.attempts), c.created_at, now, deps.schedule, deps.window
        )
        params = {
            "i": c.id,
            "started": now,
            "cls": RECIPIENTS_UNRESOLVED,
            "code": None,
            "mid": None,
            "nb": nb,
        }
        await conn.execute(_RETRY if nb is not None else _FAIL, params)
        await _audit(
            conn,
            AssessAuditEvent.DELIVERY_ATTEMPT,
            t,
            reason=RECIPIENTS_UNRESOLVED,
            round=int(c.round),
            ref=c.recipient_ref,
            attempt=int(c.attempts),
            outcome="retrying" if nb is not None else "failed",
        )
        if nb is None:
            await _audit(
                conn,
                AssessAuditEvent.DELIVERY_FAILED,
                t,
                reason=RECIPIENTS_UNRESOLVED,
                round=int(c.round),
                ref=c.recipient_ref,
            )
            out.failed += 1
        else:
            out.retrying += 1
    if claimed:
        await _audit(
            conn,
            AssessAuditEvent.TRANSCRIPT_RECIPIENTS_UNRESOLVED,
            t,
            reason=RECIPIENTS_UNRESOLVED,
            rows=len(claimed),
        )


async def deliver_transcript(
    deps: DeliveryDeps, acquire: AcquireDb, tid: UUID
) -> Delivered | None:
    """Deliver the due rows of transcript ``tid``. None: no live transcript (stale job).

    Each due row is claimed (attempt start committed) only once it holds a relay session
    (bounded wait; a row that gets none stays untouched and due), and the transcript bytes
    are read only after the first claim that returned a row.
    """
    out = Delivered()
    now = deps.clock()
    async with acquire() as conn:
        t = (await conn.execute(_TRANSCRIPT, {"t": tid})).first()
        await conn.rollback()
    if t is None or not t.live:
        return None  # never generated, or shredded: plan 12 owns ABANDONED
    by_ref: dict[str, str] = {}
    due: list[Any] = []
    async with acquire() as conn, conn.begin():
        configured = await deps.recipients.read(conn, t.site_id, int(t.cmid))
        await conn.execute(_LOCK, {"t": str(t.id)})
        if not configured:
            await _unresolved(conn, deps, t, now, out)
        else:
            by_ref = {recipient_ref(t.site_id, a): a for a in configured}
            await _fan_out(conn, t, list(by_ref), now, out)
            due = (
                await conn.execute(
                    _DUE_IDS, {"t": t.id, "now": now, "stale": now - deps.stale_after}
                )
            ).all()
    del configured  # the addresses stay in by_ref, local to this call
    if out.unresolved:
        if out.retrying or out.failed:
            _alert(RECIPIENTS_UNRESOLVED, session_id=str(t.id), count=out.retrying)
        return out
    if not due:
        return out
    docs = _Doc(deps, acquire, t)
    await _all(*(_row(deps, acquire, t, d, by_ref, docs, out) for d in due))
    log.info(
        "assess_delivery_round",
        session_id=str(t.id),
        count=len(due),
        outcome=(
            f"sent={out.sent},retrying={out.retrying},failed={out.failed},"
            f"busy={out.busy}"
        ),
    )
    return out


class _Doc:
    """The transcript bytes, read and re-hashed once, on the first claim that needs them."""

    def __init__(self, deps: DeliveryDeps, acquire: AcquireDb, t: Any) -> None:
        self._deps, self._acquire, self._t = deps, acquire, t
        self._lock = asyncio.Lock()
        self._doc: bytes | None = None
        self._error: str | None = None

    async def get(self) -> bytes | str:
        """The bytes, or the error class (``transcript_key`` / ``transcript_integrity``)."""
        async with self._lock:
            if self._doc is None and self._error is None:
                await self._load()
            return self._doc if self._doc is not None else str(self._error)

    async def _load(self) -> None:
        t = self._t
        async with self._acquire() as conn:
            row = (await conn.execute(_DOC, {"t": t.id})).first()
            await conn.rollback()
        try:
            if row is None:
                raise store.TranscriptIntegrityError("shredded since")
            self._doc = store.open_verified(
                self._deps.transcript_kek,
                t.id,
                bytes(row.ciphertext),
                bytes(row.dek_wrapped),
                row.content_hash,
            )
        except store.TranscriptKeyUnavailable:
            self._error = TRANSCRIPT_KEY
            _alert(TRANSCRIPT_KEY, session_id=str(t.id))
        except store.TranscriptIntegrityError:
            # never mail bytes that do not match their hash: final, audited, alerted
            self._error = INTEGRITY
            _alert(INTEGRITY, session_id=str(t.id))


async def _claim(
    deps: DeliveryDeps, acquire: AcquireDb, t: Any, row_id: UUID, out: Delivered
) -> _Claimed | None:
    """Commit the attempt start of ONE row (OQ-029); None when it is no longer due."""
    now = deps.clock()
    async with acquire() as conn, conn.begin():
        c = (
            await conn.execute(
                _CLAIM_ONE, {"i": row_id, "now": now, "stale": now - deps.stale_after}
            )
        ).first()
        if c is None:
            return None  # another job took it, or it changed
        if c.prev is not None:
            # an attempt started and left no outcome: maybe the relay holds it already
            await _audit(
                conn,
                AssessAuditEvent.DELIVERY_POSSIBLE_DUPLICATE,
                t,
                round=int(c.round),
                ref=c.recipient_ref,
                delivery_id=str(c.id),
            )
            out.duplicates += 1
    return _Claimed(
        c.id,
        int(c.round),
        c.recipient_ref,
        int(c.attempts),
        c.created_at,
        now,
        c.prev is not None,
    )


async def _row(
    deps: DeliveryDeps,
    acquire: AcquireDb,
    t: Any,
    due: Any,
    by_ref: dict[str, str],
    docs: _Doc,
    out: Delivered,
) -> None:
    address = by_ref.get(due.recipient_ref or "")
    if address is None or not domain_allowed(address, deps.domains):
        # nothing to connect for: claimed and settled without a relay session
        c = await _claim(deps, acquire, t, due.id, out)
        if c is None:
            return
        if address is None:
            # the row was in flight during the fan-out and its recipient is gone since
            reason, final = RECIPIENTS_UNRESOLVED, False
        else:
            reason, final = DOMAIN_NOT_ALLOWED, True
        await _settle(
            deps, acquire, t, c, SendResult(False), reason, None, out, final=final
        )
        return
    try:
        async with deps.sender.slot():
            c = await _claim(deps, acquire, t, due.id, out)
            if c is None:
                return
            doc = await docs.get()
            if isinstance(doc, str):
                await _settle(
                    deps,
                    acquire,
                    t,
                    c,
                    SendResult(False),
                    doc,
                    None,
                    out,
                    final=doc == INTEGRITY,
                )
                return
            await _send(deps, acquire, t, c, address, doc, out)
    except SlotBusy:
        out.busy += 1  # no relay session in time: the row stays due, untouched


async def _send(
    deps: DeliveryDeps,
    acquire: AcquireDb,
    t: Any,
    c: _Claimed,
    address: str,
    doc: bytes,
    out: Delivered,
) -> None:
    mid = mail.message_id(t.id, c.round, c.ref or "", deps.message_id_domain)
    # the short name read at close (sanitised for the header by ``message``)
    course = t.course_shortname or "corso non disponibile"
    msg = mail.build(
        mail.TranscriptMail(
            sender=deps.sender_address,
            recipient=address,
            course=course,
            cmid=int(t.cmid),
            student_userid=int(t.student_userid),
            session_no=int(t.session_no),
            filename=str(t.filename),
            attachment=doc,
            message_id=mid,
            date=deps.clock(),
        )
    )
    result = await deps.sender.send(msg, address, slot_held=True)
    del address, msg
    await _settle(deps, acquire, t, c, result, None, mid, out)


async def _settle(
    deps: DeliveryDeps,
    acquire: AcquireDb,
    t: Any,
    c: _Claimed,
    result: SendResult,
    reason: str | None,
    mid: str | None,
    out: Delivered,
    *,
    final: bool = False,
) -> None:
    """Commit one attempt's outcome, fenced on its own start time, with its audit rows."""
    now = deps.clock()
    cls = reason or (result.error_class.value if result.error_class else None)
    base = {"i": c.id, "started": c.started, "mid": mid, "code": result.smtp_code}
    if result.ok:
        stmt, params, outcome = _SENT, {**base, "now": now}, "sent"
    else:
        nb = (
            None
            if final or (result.error_class is not None and not result.retryable)
            else next_not_before(
                c.attempts, c.created_at, now, deps.schedule, deps.window
            )
        )
        if nb is None:
            stmt, params, outcome = _FAIL, {**base, "cls": cls}, "failed"
        else:
            stmt, params, outcome = _RETRY, {**base, "cls": cls, "nb": nb}, "retrying"
    ids: dict[str, Any] = {"round": c.round, "ref": c.ref, "delivery_id": str(c.id)}
    async with acquire() as conn, conn.begin():
        done = (await conn.execute(stmt, params)).rowcount
        if not done:
            # another worker took the row over (our attempt went stale): its outcome rules
            log.warning("assess_delivery_fence_lost", session_id=str(t.id))
            return
        await _audit(
            conn,
            AssessAuditEvent.DELIVERY_ATTEMPT,
            t,
            reason=cls,
            attempt=c.attempts,
            outcome=outcome,
            smtp_code=result.smtp_code,
            duplicate=c.duplicate,
            **ids,
        )
        if outcome == "sent":
            await _audit(conn, AssessAuditEvent.DELIVERY_SENT, t, **ids)
        elif outcome == "failed":
            await _audit(conn, AssessAuditEvent.DELIVERY_FAILED, t, reason=cls, **ids)
    if outcome == "sent":
        out.sent += 1
    elif outcome == "failed":
        out.failed += 1
        _alert("delivery_failed", session_id=str(t.id), reason_code=cls)
    else:
        out.retrying += 1
    if result.error_class in ALERTING:
        _alert(f"smtp_{result.error_class.value}", session_id=str(t.id))


def make_deliver_handler(
    deps: DeliveryDeps,
) -> Callable[[Any], Awaitable[None]]:
    async def handle(ctx: Any) -> None:
        try:
            tid = UUID(str(ctx.args["transcript_id"]))
        except (KeyError, ValueError):
            return  # a malformed job is a stale no-op
        await deliver_transcript(deps, ctx.acquire, tid)

    return handle


# --- the delivery_retry tick ------------------------------------------------------------------

_DUE = text(
    "SELECT DISTINCT d.transcript_id FROM assess_deliveries d WHERE d.kind = 'transcript' "
    "AND d.status IN ('PENDING', 'RETRYING') AND d.not_before <= :now "
    "AND (d.attempt_started_at IS NULL OR d.attempt_started_at < :stale) LIMIT :n"
)
_NO_ROWS = text(
    "SELECT t.id FROM assess_transcripts t WHERE t.ciphertext IS NOT NULL "
    "AND t.generated_at > :since AND NOT EXISTS (SELECT 1 FROM assess_deliveries d "
    "WHERE d.transcript_id = t.id AND d.kind = 'transcript') LIMIT :n"
)
#: a transcript with no recipient yet is looked at again at most this often
FAN_OUT_EVERY = timedelta(minutes=5)


async def delivery_retry_tick(
    acquire: AcquireDb,
    *,
    stale_after: timedelta,
    window: timedelta,
    now: datetime | None = None,
    batch: int = 200,
) -> dict[str, int]:
    """Queue a delivery job for every transcript with due rows (deduped per transcript), and,
    at most every ``FAN_OUT_EVERY``, for a recent transcript that has no delivery row yet."""
    at = now or datetime.now(UTC)
    queued = fanout = 0
    async with acquire() as conn, conn.begin():
        due = (
            await conn.execute(_DUE, {"now": at, "stale": at - stale_after, "n": batch})
        ).all()
        for row in due:
            queued += await enqueue_job(
                conn,
                JobKind.TRANSCRIPT_DELIVER,
                {"transcript_id": str(row.transcript_id)},
                dedupe_key=f"transcript_deliver:{row.transcript_id}:retry",
            )
        none = (await conn.execute(_NO_ROWS, {"since": at - window, "n": batch})).all()
        for row in none:
            fanout += await enqueue_job(
                conn,
                JobKind.TRANSCRIPT_DELIVER,
                {"transcript_id": str(row.id)},
                dedupe_key=f"transcript_deliver:{row.id}:fanout",
                not_before=at + FAN_OUT_EVERY,
            )
    if queued or fanout:
        log.info("assess_delivery_retry", count=queued, requeued=fanout)
    return {"queued": queued, "fanout": fanout}
