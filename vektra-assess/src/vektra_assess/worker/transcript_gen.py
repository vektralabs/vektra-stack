"""``transcript_generate`` (worker job): one inert, sealed transcript per CLOSED session (REQ-074, REQ-102).

Enqueued by the close function in the CLOSED transaction (``args.session_id``). Idempotent per
session: an existing transcript makes the job a no-op, and the insert is ``ON CONFLICT
(session_id) DO NOTHING``, so two concurrent jobs yield one row.

1. Read the session (CLOSED, else a stale no-op), its wrapped DEK, snapshot, turns, flags, turn
   notes, outage intervals, earlier sessions and failed starts.
2. Re-verify: the snapshot must decrypt and re-hash to the chain's genesis (``content_hash``
   column included), and EVERY turn must re-hash to the hash persisted at turn time. Any break:
   no transcript, an ``assess_transcript_integrity_failed`` audit row (reason code and first
   broken turn only), an ERROR ``assess.transcript.alert`` log line, and the job goes dead at
   once (``PermanentJobError("integrity_mismatch")``): a transcript that does not match its
   turns is never generated, let alone delivered.
3. Read the close facts from Moodle (course short name, the grade as the student sees it now),
   outside any transaction; each part may be unreadable. Read the transcript recipients from the
   activity's sealed teacher config (REQ-082 as amended: 1..3 teacher-entered addresses).
4. Render, run the allowlist (a violation is permanent too: fail closed), seal under a fresh DEK
   wrapped by the transcript key.
5. ONE transaction: the transcript row (its id IS the session id, so the discussion UI, which
   knows only its session, can name it), one PENDING delivery per recipient (<= 3, keyed by
   ``recipient_ref``, never the address), the ``assess_transcript_generated`` audit row, and a
   deduped ``transcript_deliver`` job (whose NOTIFY wakes the worker). Recipients unreadable or
   none configured: the transcript is still generated (downloadable, NFR-021) with no delivery
   row, audited ``recipients_unresolved`` + alert; the delivery tick fans them out later.

The worker holds the session KEK; this module never logs content and never touches SMTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from cryptography.exceptions import InvalidTag
from sqlalchemy import text

from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.context.capture.store import Mismatch, verify_snapshot_integrity
from vektra_assess.context.regrade import compare_grade
from vektra_assess.crypto import envelope
from vektra_assess.crypto.kek import KeyNotAvailable, unwrap_dek
from vektra_assess.delivery.recipients import ConfigRecipients, recipient_ref
from vektra_assess.jobs import JobKind, enqueue_job
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.language import detect
from vektra_assess.transcript import allowlist, naming, render, store
from vektra_assess.transcript.facts import CloseFacts, CloseFactsSource
from vektra_assess.transcript.hashing import (
    ChainError,
    ChainTurn,
    turns_digest,
    verify_turns,
)
from vektra_assess.worker.runner import AcquireDb, JobContext
from vektra_assess.worker.runtime import PermanentJobError
from vektra_shared.lms import Unreadable, UnreadableReason

log = structlog.get_logger(__name__)


class IntegrityFailure(PermanentJobError):
    """No transcript: the job goes dead at once with ``integrity_mismatch`` (never retried)."""

    def __init__(self, reason: str, turn_no: int | None = None) -> None:
        super().__init__("integrity_mismatch")
        self.reason = reason
        self.turn_no = turn_no


class TranscriptKeyUnavailable(RuntimeError):
    """The session's DEK is wrapped under a KEK kid this worker does not hold: retryable."""


@dataclass(frozen=True)
class GenerationDeps:
    kek: AssessKeyring  # assess_kek: unwraps the session DEK (worker only)
    transcript_kek: AssessKeyring  # assess_transcript_kek: seals the transcript
    facts: CloseFactsSource | None = None
    #: the activity's configured recipients (sealed teacher config, worker private key)
    recipients: ConfigRecipients | None = None
    tz: str = "Europe/Rome"
    br009_template: str = render.BR009_TEMPLATE
    #: Moodle's share of the 30 s job budget (review #4): past it the header says "non
    #: disponibile" and the transcript is still generated, well inside the budget
    facts_timeout_s: float = 12.0


@dataclass(frozen=True)
class Generated:
    transcript_id: UUID
    recipients: int
    created: bool


_SESSION = text(
    "SELECT s.id, s.site_id, s.cmid, s.student_userid, s.session_no, s.state, s.close_reason, "
    "s.courseid_hint, s.duration_s, s.committed_at, s.closed_at, s.created_at, s.frozen_policy, "
    "s.credited_outage_s, s.cap_s, s.seq, c.activity_type, c.cap_minutes, "
    "(SELECT count(*) FROM assess_sessions x WHERE x.site_id = s.site_id AND x.cmid = s.cmid "
    "AND x.student_userid = s.student_userid) AS sessions_total, "
    "(SELECT count(*) FROM assess_start_attempts a WHERE a.site_id = s.site_id "
    "AND a.cmid = s.cmid AND a.student_userid = s.student_userid "
    "AND a.outcome = 'rolled_back' AND a.created_at <= s.created_at) AS failed_starts, "
    # a purged session is done for good (plan 12): its tombstone outlives the transcript, so a
    # retried job never regenerates what the retention purge removed
    "(EXISTS (SELECT 1 FROM assess_transcripts t WHERE t.session_id = s.id) "
    "OR EXISTS (SELECT 1 FROM assess_purge_records p WHERE p.session_id = s.id)) AS done "
    "FROM assess_sessions s LEFT JOIN assess_activity_config c ON c.site_id = s.site_id "
    "AND c.cmid = s.cmid WHERE s.id = :sid"
)
_KEY = text(
    "SELECT kek_wrapped, key_version FROM assess_session_keys WHERE session_id = :sid "
    "AND shredded_at IS NULL"
)
_TURNS = text(
    "SELECT t.turn_no, t.role, t.kind, t.ciphertext, t.turn_hash, t.prev_hash, t.created_at, "
    "(f.turn_no IS NOT NULL) AS flagged, n.marks, n.contests, n.lang "
    "FROM assess_turns t LEFT JOIN assess_turn_flags f ON f.session_id = t.session_id "
    "AND f.turn_no = t.turn_no LEFT JOIN assess_turn_notes n ON n.session_id = t.session_id "
    "AND n.turn_no = t.turn_no WHERE t.session_id = :sid ORDER BY t.turn_no"
)
_OUTAGES = text(
    "SELECT started_at, ended_at, credited_s FROM assess_outage_incidents "
    "WHERE session_id = :sid ORDER BY started_at"
)
_EARLIER = text(
    "SELECT session_no, closed_at, close_reason FROM assess_sessions WHERE site_id = :site "
    "AND cmid = :cmid AND student_userid = :u AND session_no < :n AND state = 'CLOSED' "
    "ORDER BY session_no"
)
_LOCK_SESSION = text("SELECT 1 FROM assess_sessions WHERE id = :sid FOR KEY SHARE")
_INSERT = text(
    "INSERT INTO assess_transcripts (id, session_id, ciphertext, dek_wrapped, transcript_kid, "
    "content_hash, turns_digest, filename, byte_size, course_shortname) SELECT :id, :sid, :ct, "
    ":dek, :kid, :h, :digest, :name, :size, :course WHERE NOT EXISTS (SELECT 1 FROM "
    "assess_purge_records p WHERE p.session_id = :sid) ON CONFLICT (session_id) DO NOTHING "
    "RETURNING id"
)
_DELIVERY = text(
    "INSERT INTO assess_deliveries (transcript_id, kind, round, recipient_kind, "
    "recipient_ref, status) VALUES (:t, 'transcript', 1, 'address', :ref, 'PENDING')"
)


def _alert(event: str, **fields: Any) -> None:
    # ERROR level, class and ids only: plan 12's metrics and the operator read this event
    log.error("assess.transcript.alert", alert=event, **fields)


async def _integrity_failed(
    acquire: AcquireDb, row: Any, reason: str, turn_no: int | None
) -> None:
    async with acquire() as conn, conn.begin():
        await write_event(
            conn,
            AssessAuditEvent.TRANSCRIPT_INTEGRITY_FAILED,
            actor="worker",
            site_id=row.site_id,
            session_id=row.id,
            reason_code=reason,
            data={"turn_no": turn_no},
        )
    _alert("integrity_mismatch", session_id=str(row.id), reason=reason, turn_no=turn_no)


async def generate_transcript(
    deps: GenerationDeps,
    acquire: AcquireDb,
    sid: UUID,
    *,
    now: datetime | None = None,
    attempt: int = 1,
) -> Generated | None:
    """Generate the transcript of ``sid``; None for a stale job (not CLOSED, already done)."""
    async with acquire() as conn:
        row = (await conn.execute(_SESSION, {"sid": sid})).first()
        if row is None or row.state != "CLOSED" or row.done:
            return None
        key = (await conn.execute(_KEY, {"sid": sid})).first()
        turn_rows = (await conn.execute(_TURNS, {"sid": sid})).all()
        outages = (await conn.execute(_OUTAGES, {"sid": sid})).all()
        earlier = (
            await conn.execute(
                _EARLIER,
                {
                    "site": row.site_id,
                    "cmid": row.cmid,
                    "u": row.student_userid,
                    "n": row.session_no,
                },
            )
        ).all()
        await conn.rollback()
    if key is None or key.kek_wrapped is None or not turn_rows:
        await _integrity_failed(acquire, row, "missing_material", None)
        raise IntegrityFailure("missing_material")
    try:
        dek = unwrap_dek(deps.kek, bytes(key.kek_wrapped), sid, int(key.key_version))
    except KeyNotAvailable:
        # a configuration or rotation problem, not tampering (review #8): the job is retried
        # with the normal backoff, the operator is alerted once, nothing is audited as integrity
        if attempt == 1:
            _alert("kek_not_available", session_id=str(sid))
        raise TranscriptKeyUnavailable("kek_not_available") from None
    except (InvalidTag, envelope.EnvelopeError):
        await _integrity_failed(acquire, row, "dek_unwrap_failed", None)
        raise IntegrityFailure("dek_unwrap_failed") from None
    genesis = str(turn_rows[0].prev_hash)
    async with acquire() as conn, conn.begin():
        snap = await verify_snapshot_integrity(conn, sid, dek, genesis)
    if isinstance(snap, Mismatch):
        # the snapshot check audited its own event; the transcript refusal is audited too, under
        # the one event every integrity refusal of generation writes (with the alert)
        reason = "snapshot_" + snap.reason.value
        await _integrity_failed(acquire, row, reason, None)
        raise IntegrityFailure(reason)
    chain: list[ChainTurn] = []
    records: list[render.TurnRecord] = []
    try:
        for t in turn_rows:
            plain = envelope.decrypt_turn(dek, sid, int(t.turn_no), bytes(t.ciphertext))
            chain.append(
                ChainTurn(
                    int(t.turn_no), t.role, t.kind, plain, t.prev_hash, t.turn_hash
                )
            )
            records.append(_record(t, plain.decode("utf-8")))
        verify_turns(genesis, chain)
        if len(chain) != int(row.seq):
            # a deleted trailing turn leaves a prefix that still verifies: the session's own
            # turn counter must agree
            await _integrity_failed(acquire, row, "turn_count_mismatch", len(chain))
            raise IntegrityFailure("turn_count_mismatch", len(chain))
    except ChainError as exc:
        await _integrity_failed(acquire, row, "turn_hash_mismatch", exc.turn_no)
        raise IntegrityFailure("turn_hash_mismatch", exc.turn_no) from None
    except (InvalidTag, envelope.EnvelopeError, UnicodeDecodeError):
        await _integrity_failed(acquire, row, "turn_decrypt_failed", None)
        raise IntegrityFailure("turn_decrypt_failed") from None
    finally:
        del dek

    facts = await _close_facts(deps, row)
    at = now or datetime.now(UTC)
    inp = render.TranscriptInput(
        course=facts.course_shortname,
        cmid=int(row.cmid),
        student_userid=int(row.student_userid),
        session_no=int(row.session_no),
        sessions_total=int(row.sessions_total),
        earlier=tuple(
            render.EarlierSession(int(e.session_no), e.closed_at, e.close_reason)
            for e in earlier
        ),
        previous_context=bool((row.frozen_policy or {}).get("include_previous_turns")),
        snapshot=snap.snapshot,
        regrade=compare_grade(
            snap.snapshot,
            facts.grade
            if facts.grade is not None
            else Unreadable(UnreadableReason.PARTIAL, "not_read"),
        ),
        # frozen at the commit; the configuration's value only for sessions committed before
        # revision 0034
        cap_minutes=(int(row.cap_s) // 60) if row.cap_s else row.cap_minutes,
        duration_s=row.duration_s,
        committed_at=row.committed_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        outages=tuple(
            render.Outage(o.started_at, o.ended_at, int(o.credited_s)) for o in outages
        ),
        credited_s=int(row.credited_outage_s or 0),
        failed_starts=int(row.failed_starts),
        turns=tuple(records),
        digest=turns_digest([c.turn_hash for c in chain]),
        generated_at=at,
        tz=deps.tz,
        br009_template=deps.br009_template,
    )
    rendered = render.render(render.build_doc(inp))
    try:
        allowlist.check(rendered)
    except allowlist.AllowlistViolation as exc:
        await _integrity_failed(acquire, row, "allowlist_violation", None)
        raise IntegrityFailure("allowlist_violation") from exc
    tid = sid  # the transcript id IS the session id (1:1, UNIQUE session_id)
    sealed = store.seal(deps.transcript_kek, tid, rendered)
    name = naming.transcript_filename(
        facts.course_shortname,
        int(row.cmid),
        int(row.student_userid),
        int(row.session_no),
        row.closed_at,
        deps.tz,
    )
    configured: tuple[str, ...] | None = None
    if deps.recipients is not None:
        async with acquire() as conn:
            configured = await deps.recipients.read(conn, row.site_id, int(row.cmid))
            await conn.rollback()
    refs = [recipient_ref(row.site_id, a) for a in configured or ()]
    unresolved = "config_unreadable" if configured is None else "no_recipients"
    del configured  # only the refs leave this point
    async with acquire() as conn, conn.begin():
        # FOR KEY SHARE waits for a purge holding the session row (FOR UPDATE); the INSERT is
        # then a new statement with a new snapshot, so it sees that purge's tombstone and
        # inserts nothing (the check alone used a snapshot from before
        # the purge committed)
        await conn.execute(_LOCK_SESSION, {"sid": sid})
        got = (
            await conn.execute(
                _INSERT,
                {
                    "id": tid,
                    "sid": sid,
                    "ct": sealed.ciphertext,
                    "dek": sealed.dek_wrapped,
                    "kid": sealed.kid,
                    "h": sealed.content_sha256,
                    "digest": inp.digest,
                    "name": name,
                    "size": sealed.byte_size,
                    "course": facts.course_shortname,
                },
            )
        ).scalar()
        if got is None:
            return Generated(tid, 0, False)  # a concurrent job won: one row per session
        for ref in refs:
            await conn.execute(_DELIVERY, {"t": tid, "ref": ref})
        await write_event(
            conn,
            AssessAuditEvent.TRANSCRIPT_GENERATED,
            actor="worker",
            site_id=row.site_id,
            session_id=sid,
            data={
                "turns": len(chain),
                "bytes": sealed.byte_size,
                "recipients": len(refs),
            },
        )
        if not refs:
            await write_event(
                conn,
                AssessAuditEvent.TRANSCRIPT_RECIPIENTS_UNRESOLVED,
                actor="worker",
                site_id=row.site_id,
                session_id=sid,
                reason_code=unresolved,
            )
        if refs:
            await enqueue_job(
                conn,
                JobKind.TRANSCRIPT_DELIVER,
                {"transcript_id": str(tid)},
                dedupe_key=f"transcript_deliver:{tid}:1",
            )
    if not refs:
        _alert("recipients_unresolved", session_id=str(sid), reason_code=unresolved)
    log.info(
        "assess_transcript_generated",
        session_id=str(sid),
        count=len(refs),
        bytes=sealed.byte_size,
    )
    return Generated(tid, len(refs), True)


def _record(t: Any, text_: str) -> render.TurnRecord:
    """A student turn's language is detected from its text with the runner's own detector; a
    bot turn's is the reply language its note recorded; the opening turns are Italian."""
    lang = t.lang if t.role == "bot" else detect(text_)
    if t.kind == "opening":
        lang = "it"
    return render.TurnRecord(
        no=int(t.turn_no),
        role=str(t.role),
        kind=str(t.kind),
        text=text_,
        at=t.created_at,
        flagged=bool(t.flagged),
        marks=tuple(t.marks or ()),
        contests=tuple(
            (str(c.get("label", "")), str(c.get("status", "")))
            for c in (t.contests or [])
            if isinstance(c, dict)
        ),
        lang=lang,
    )


async def _close_facts(deps: GenerationDeps, row: Any) -> CloseFacts:
    if deps.facts is None:
        return CloseFacts()
    # each part lands here as soon as it is read: past the timeout the transcript keeps the
    # course and the grade already read, only the rest is "non disponibile"
    sink: dict[str, Any] = {}
    try:
        async with asyncio.timeout(deps.facts_timeout_s):
            return await deps.facts.close_facts(
                session_id=row.id,
                site_id=row.site_id,
                cmid=int(row.cmid),
                student_userid=int(row.student_userid),
                courseid_hint=row.courseid_hint,
                activity_type=str(row.activity_type or "assign"),
                sink=sink,
            )
    # Moodle down, or slower than its share (an inner TimeoutError, never the job's own
    # cancellation): what was read is kept, the rest says "non disponibile", never a guess
    except Exception as exc:
        log.warning("assess_transcript_facts_error", error_type=type(exc).__name__)
        return CloseFacts(
            course_shortname=sink.get("course_shortname"), grade=sink.get("grade")
        )


def make_generate_handler(
    deps: GenerationDeps,
) -> Callable[[JobContext], Awaitable[None]]:
    async def handle(ctx: JobContext) -> None:
        try:
            sid = UUID(str(ctx.args["session_id"]))
        except (KeyError, ValueError):
            return  # a malformed job is a stale no-op
        await generate_transcript(deps, ctx.acquire, sid, attempt=ctx.attempt)

    return handle
