"""``POST /api/v1/assess/sessions/{sid}/turns`` and the ``TurnDriver`` (CONF-007: turns run in the API).

The request runs the gate (steps 1-6, ACTIVE only), loads the session material, refuses a second
message while a reply is pending (409 "attendi la risposta"), and appends the student turn through
``assess_append_turn`` (ciphertext under the session DEK, hash chained on the last turn), which
sets ``pending_reply_since`` and refuses a second student turn itself under the row lock. The
request returns 202 at once.

The reply runs in a detached task (``TurnDriver``), independent of the HTTP connection: a client
that disconnects does not stop it. It streams the runner's released segments to the session's
``EventBroker`` as ``reply_delta`` events and, at the end, appends the bot turn (normal, or the
marked ``interrupted`` turn when the runner was stopped) with its hash, then publishes
``reply_done`` / ``reply_interrupted``. Plaintext lives only in memory: nothing is persisted chunk
by chunk.

Provider outage (M4.1, REQ-086): a ``ProviderOutageError`` moves the session ACTIVE -> OUTAGE
through ``assess_enter_outage`` (the interval starts at the failed call's own start, from its
``ProviderCallRecord``); the timer is frozen, the input locked, and the pending student turn KEPT:
the ``PendingTurnDriver`` answers it once the worker's probe ends the outage. The third consecutive
failed call closes the session ``service_failure`` instead (in the same function). An unexpected
error or a refused bot append still ends the attempt with the marked bot turn "[risposta
interrotta]" (``turn_markers``), which clears the pending marker, then ``reply_interrupted``.

A refusal of the deployment's CONFIGURATION (``llm.probe.refusal_is_configuration``: every
``auth_error``; a non-429 4xx only when the turn-shaped probe is refused too, so a context-length or
content-policy 400 of one request is not) answers the turn with the marked turn
(``reply_interrupted`` ``reason: service_misconfigured``) AND pauses the session in OUTAGE with that
reason, in ONE transaction: there is no moment with the session ACTIVE and no reply owed. The
outage probe credits the time once the provider answers again (``provider_alerts``). When the
database refuses the marked turn (past deadline + grace, the chain moved) nothing is paused. Every
config refusal, confirmed or not, queues the operator alert once per site, cause and hour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_assess.api.common import engine_of, fail, parse_body
from vektra_assess.api.deps import dek_cache_of, handoff_private_of, settings_of
from vektra_assess.api.events import OUTAGE_COUNT, EventBroker, broker_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.material import DekCache, Material, load_material
from vektra_assess.api.models import TurnRequest
from vektra_assess.api.session_gate import SessionRow, session_tx
from vektra_assess.api.site_binding import bound_site
from vektra_assess.api.state import limiter_of
from vektra_assess.audit import AssessAuditEvent, write_event
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import FrozenPolicy
from vektra_assess.context.snapshot import from_canonical_bytes
from vektra_assess.crypto import envelope, hashing
from vektra_assess.errors import (
    assess_unavailable,
    bad_request,
    close_message_code,
    deadline_passed,
    rate_limited,
    reply_pending,
    state_conflict,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.probe import probe_budget_s, refusal_is_configuration
from vektra_assess.llm.types import (
    ContestEntry,
    PriorTurn,
    ProviderCallRecord,
    ProviderOutageError,
    ReleasedSegment,
    StudentTurn,
    TurnResult,
)
from vektra_assess.provider_alerts import (
    PROVIDER_UNAVAILABLE,
    SERVICE_MISCONFIGURED,
    queue_provider_config_alert,
)
from vektra_assess.tokens import SessionClaims
from vektra_assess.turn_markers import INTERRUPTED_REPLY, REPLY_CLAIM_KEY_SQL

log = structlog.get_logger(__name__)

router = APIRouter()

#: seconds kept before deadline + grace to persist a cut reply
CUT_MARGIN_S = 2.0

# A provider error that is the provider ANSWERING (any 4xx: a rate limit, over-length, a content
# filter, a wrong key or model, a request it rejects; a missing key, which litellm reports as an
# AuthenticationError with a synthetic 500), the provider library's spend limit, or a
# max_tokens spent before any text is a refusal, not an outage: it never sets OUTAGE and earns no
# credit (REQ-086); the attempt ends with the marked turn and the student may write again. The
# decision is ``ProviderOutageError.is_refusal``, from the runner's closed cause code (built from
# type names and the received status, never the message).

_APPEND = text(
    "SELECT assess_append_turn(:sid, :epoch, :seq, :role, :kind, :ct, :h, :prev)"
)
_NOTE = text(
    "INSERT INTO assess_turn_notes (session_id, turn_no, marks, contests, lang) "
    "VALUES (:s, :n, :m, CAST(:c AS jsonb), :l)"
)


@dataclass(frozen=True)
class TurnNotes:
    """What the transcript must mark on a bot turn and no turn column holds (revision 0034):
    post-check stage codes, ``[[PUNTO_CONTESTATO]]`` entries (label + status, no text) and the
    reply language."""

    marks: tuple[str, ...]
    contests: tuple[ContestEntry, ...]
    lang: str | None


#: bounds that keep a note far below the table's CHECK (``pg_column_size(contests) < 4096``,
#: ``cardinality(marks) <= 16``): a CheckViolation would roll back the bot turn itself
MAX_NOTE_MARKS = 16
MAX_NOTE_CONTESTS = 16
MAX_NOTE_LABEL = 64
_MAX_STATUS = 16


def _cut_utf8(text_: str, max_bytes: int) -> str:
    return text_.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


#: the encoded budget of the contests JSON, with room for the JSONB header and overhead
MAX_NOTE_CONTESTS_BYTES = 3000


def turn_notes(result: TurnResult) -> TurnNotes:
    """The note of one answered turn, deduplicated and capped (review #2): marks once each;
    one contest entry per label (the LAST one wins, as in the transcript), the latest
    ``MAX_NOTE_CONTESTS`` labels, each label cut to ``MAX_NOTE_LABEL`` UTF-8 BYTES (multibyte
    text cannot inflate it), and the oldest entries dropped until the encoded JSON fits
    ``MAX_NOTE_CONTESTS_BYTES``: never over the 4096-byte CHECK."""
    latest: dict[str, str] = {}
    for entry in result.contest_entries:
        label = _cut_utf8(str(entry.label), MAX_NOTE_LABEL)
        latest.pop(label, None)
        latest[label] = _cut_utf8(str(entry.status), _MAX_STATUS)
    kept = list(latest.items())[-MAX_NOTE_CONTESTS:]
    while (
        kept
        and len(
            json.dumps(
                [{"label": k, "status": v} for k, v in kept], ensure_ascii=False
            ).encode("utf-8")
        )
        > MAX_NOTE_CONTESTS_BYTES
    ):
        kept.pop(0)
    return TurnNotes(
        marks=tuple(dict.fromkeys(_cut_utf8(str(m), 32) for m in result.marks))[
            :MAX_NOTE_MARKS
        ],
        contests=tuple(ContestEntry(label, status) for label, status in kept),  # type: ignore[arg-type]
        lang=result.language if result.language in ("it", "en", "other") else None,
    )


_PENDING_TURN = text(
    "SELECT seq, pending_reply_since FROM assess_sessions "
    "WHERE id = :s AND state = 'ACTIVE' AND epoch = :e"
)
_ADD_TOKENS = text(
    "UPDATE assess_sessions SET tokens_used = tokens_used + :n WHERE id = :s "
    "RETURNING tokens_used"
)
_CLOSE_BUDGET = text("SELECT assess_close_session(:sid, :epoch, 'budget_exhausted')")
_ENTER_OUTAGE = text("SELECT assess_enter_outage(:sid, :epoch, :started, :reason)")
_AUDIT = text(
    "INSERT INTO assess_audit_events (event, reason_code, actor_kind, data) "
    "VALUES (:event, :reason, 'api', CAST(:data AS jsonb))"
)


class ApiAuditSink:
    """The runner's ``AuditSink`` in the API: one content-free row per call, own transaction."""

    def __init__(self, engine: Any) -> None:
        self._engine = (
            engine  # an AsyncEngine, or a zero-argument callable returning one
        )

    async def __call__(self, event: str, data: Mapping[str, Any]) -> None:
        engine = self._engine() if callable(self._engine) else self._engine
        reason = data.get("reason_code")
        async with engine.begin() as conn:
            await conn.execute(
                _AUDIT,
                {
                    "event": event,
                    "reason": reason if isinstance(reason, str) else None,
                    "data": json.dumps(dict(data), sort_keys=True, default=str),
                },
            )


def bundle_of(material: Material, row: SessionRow) -> ContextBundle:
    """The frozen bundle of a live session: snapshot decrypted, projected by its frozen policy."""
    if row.frozen_policy is None:
        raise ValueError("a committed session always has a frozen policy")
    plaintext = envelope.decrypt_snapshot(
        material.dek, material.session_id, material.snapshot_ciphertext
    )
    return assemble_context(
        from_canonical_bytes(plaintext), FrozenPolicy.model_validate(row.frozen_policy)
    )


def history_of(material: Material) -> tuple[PriorTurn, ...]:
    return tuple(
        PriorTurn(
            "student" if t.role == "student" else "assistant", material.text_of(t)
        )
        for t in material.turns
    )


async def append_turn(
    conn: Any,
    material: Material,
    *,
    epoch: int,
    seq: int,
    role: str,
    kind: str,
    text_: str,
) -> int | None:
    """Encrypt and append one turn after the material's last; None when the function refuses."""
    turn_no = material.next_turn_no
    plaintext = text_.encode("utf-8")
    prev = material.last_hash
    digest = hashing.turn_hash(prev, turn_no, role, kind, plaintext)
    got = (
        await conn.execute(
            _APPEND,
            {
                "sid": material.session_id,
                "epoch": epoch,
                "seq": seq,
                "role": role,
                "kind": kind,
                "ct": envelope.encrypt_turn(
                    material.dek, material.session_id, turn_no, plaintext
                ),
                "h": digest,
                "prev": prev,
            },
        )
    ).scalar()
    return int(got) if got is not None else None


@dataclass
class TurnDriver:
    """Detached reply tasks, one per session at most (the pending-reply rule guarantees it)."""

    engine: AsyncEngine
    broker: EventBroker
    runner: Any  # DiscussionTurnRunner (or a test double with ``run_stream``)
    handoff_private: AssessKeyring
    cache: DekCache
    #: REQ-083 session token budget (``VEKTRA_ASSESS_SESSION_TOKEN_BUDGET``); None: unlimited
    token_budget: int | None = None
    tasks: dict[UUID, asyncio.Task[None]] = field(default_factory=dict)
    _slots: asyncio.Semaphore | None = None

    def reply_slots(self) -> int:
        """How many replies may run at once (review fix 2). A reply holds its claim connection
        for its whole life and needs a second one to persist, so at most half of the pool goes
        to replies, after a margin (a quarter of the pool, at least one connection) kept for
        every other request. The extra replies wait for a slot; they never wait holding one
        connection for another."""
        pool = self.engine.pool
        size = getattr(pool, "size", None)
        total = (size() if callable(size) else 5) + max(
            int(getattr(pool, "_max_overflow", 0) or 0), 0
        )
        margin = max(1, total // 4)
        return max(1, (total - margin) // 2)

    @property
    def slots(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.reply_slots())
        return self._slots

    def start(
        self,
        sid: UUID,
        epoch: int,
        bundle: ContextBundle,
        turn: StudentTurn,
        cut_after_s: float | None = None,
        expected_seq: int | None = None,
    ) -> asyncio.Task[None]:
        """``cut_after_s``: seconds until deadline + grace (minus the persist margin): the reply
        is stopped then and the released part is persisted as a marked interrupted turn.
        ``expected_seq``: the session's ``seq`` right after the student turn this reply
        answers; once the claim is held the turn is checked to be still that pending one."""
        # the cut is an absolute time: time spent waiting for
        # a slot or the claim is not added to it
        cut_at = (
            asyncio.get_running_loop().time() + cut_after_s
            if cut_after_s is not None
            else None
        )
        task = asyncio.create_task(
            self._drive(sid, epoch, bundle, turn, cut_at, expected_seq)
        )
        self.tasks[sid] = task
        task.add_done_callback(
            lambda t: self.tasks.pop(sid, None) if self.tasks.get(sid) is t else None
        )
        return task

    @asynccontextmanager
    async def _claim(self, sid: UUID) -> AsyncIterator[bool]:
        """The live-reply claim for the reply's lifetime: a session-level advisory lock taken
        with ``pg_try_advisory_lock`` on a dedicated connection. Yields whether it was taken.

        Never blocking and never optional: a driver that does not get the claim (another driver,
        here or in another API process, is answering this turn) or cannot take it (database
        unreachable) does not reply. The holder answers; a turn nobody holds is answered by the
        ``PendingTurnDriver`` or marked by the worker's ``reconcile_reply``. Together with the
        database guard (revision 0031: a bot turn needs a pending student turn) a student turn
        gets exactly one bot turn."""
        conn = None
        claimed = False
        # whether the session may still hold the lock in a way we could not undo: such a
        # connection is invalidated (closed for good), never returned to the pool, where the
        # session-level lock would survive and block the turn
        uncertain = False
        try:
            conn = await self.engine.connect()
            claimed = bool(
                (
                    await conn.execute(
                        text(f"SELECT pg_try_advisory_lock({REPLY_CLAIM_KEY_SQL})"),
                        {"sid": str(sid)},
                    )
                ).scalar()
            )
            await conn.commit()
        except Exception as exc:
            log.warning("assess_reply_claim_failed", error_type=type(exc).__name__)
            uncertain = conn is not None
            claimed = False
        try:
            yield claimed
        finally:
            if conn is not None:
                try:
                    if claimed:
                        unlocked = (
                            await conn.execute(
                                text(
                                    f"SELECT pg_advisory_unlock({REPLY_CLAIM_KEY_SQL})"
                                ),
                                {"sid": str(sid)},
                            )
                        ).scalar()
                        await conn.commit()
                        uncertain = uncertain or not unlocked
                except Exception:
                    uncertain = True
                finally:
                    if uncertain:
                        with contextlib.suppress(Exception):
                            await conn.invalidate()
                    await conn.close()

    async def _drive(
        self,
        sid: UUID,
        epoch: int,
        bundle: ContextBundle,
        turn: StudentTurn,
        cut_at: float | None = None,
        expected_seq: int | None = None,
    ) -> None:
        """``cut_at``: the loop time of deadline + grace minus the persist margin."""
        async with self.slots, self._claim(sid) as claimed:
            if not claimed:
                log.info("assess_reply_not_claimed")  # another driver answers this turn
                return
            # the claim may come after another driver answered: generate only while this very student turn is still pending
            if expected_seq is not None and not await self._still_pending(
                sid, epoch, expected_seq
            ):
                log.info("assess_reply_stale")
                return
            remaining = (
                cut_at - asyncio.get_running_loop().time()
                if cut_at is not None
                else None
            )
            await self._reply(sid, epoch, bundle, turn, remaining)

    async def _still_pending(self, sid: UUID, epoch: int, expected_seq: int) -> bool:
        async with self.engine.connect() as conn:
            row = (await conn.execute(_PENDING_TURN, {"s": sid, "e": epoch})).first()
        return bool(
            row is not None
            and row.pending_reply_since is not None
            and int(row.seq) == expected_seq
        )

    async def _reply(
        self,
        sid: UUID,
        epoch: int,
        bundle: ContextBundle,
        turn: StudentTurn,
        cut_after_s: float | None = None,
    ) -> None:
        result: TurnResult | None = None
        failure: str | None = None
        outage: ProviderCallRecord | None = None
        #: a configuration refusal: the marked turn first, then OUTAGE with this reason
        misconfigured: ProviderCallRecord | None = None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        # the reply's cut on the loop clock: the confirming probe never runs past it
        cut_at = loop.time() + cut_after_s if cut_after_s is not None else None
        cut = (
            asyncio.get_running_loop().call_later(max(cut_after_s, 0.0), stop.set)
            if cut_after_s is not None
            else None
        )
        try:
            async for item in self.runner.run_stream(bundle, turn, stop):
                if isinstance(item, ReleasedSegment):
                    self.broker.publish(
                        sid, "reply_delta", {"text": item.text}, epoch=epoch
                    )
                elif isinstance(item, TurnResult):
                    result = item
        except ProviderOutageError as exc:
            if exc.is_refusal:
                failure = "provider_refused"
                if exc.is_config_refusal:
                    await self._alert_config_refusal(sid, exc.cause)
                if await self._is_configuration(
                    exc, probe_budget_s(cut_at, loop.time())
                ):
                    failure = SERVICE_MISCONFIGURED
                    misconfigured = exc.record
            else:
                # the server-side record: the interval starts at the call's own start
                outage = exc.record
            log.warning(
                "assess_turn_provider_failure",
                session_id=str(sid),
                # the log keeps its two codes: a configuration refusal is a refusal
                reason_code="provider_refused"
                if exc.is_refusal
                else "provider_unavailable",
                **exc.log_fields(getattr(self.runner, "max_tokens", None)),
            )
        except Exception as exc:
            log.warning("assess_turn_driver_error", error_type=type(exc).__name__)
            failure = "internal_error"
        finally:
            if cut is not None:
                cut.cancel()
        if outage is None and failure is None and result is None:
            failure = "internal_error"
        try:
            if outage is not None:
                await self._outage(sid, epoch, outage)
            elif failure is not None or result is None:
                # a configuration refusal also pauses the student's time, in the marker's
                # own transaction (credited like an outage by the probe)
                await self._interrupted(
                    sid, epoch, failure or "internal_error", pause=misconfigured
                )
            else:
                await self._persist(sid, epoch, result)
        except Exception as exc:
            # a lost hand-off, an unseal or a database failure while persisting: reported to the
            # stream and logged by type only; the task never ends with an unread exception
            log.warning("assess_turn_persist_failed", error_type=type(exc).__name__)
            self.broker.publish(sid, "reply_interrupted", {"reason": "persist_failed"})

    async def _alert_config_refusal(self, sid: UUID, cause: str) -> None:
        """The operator alert of a configuration refusal (``provider_alerts``), in its own
        transaction. The site is the one this reply task is bound to (the request's, or the
        ``PendingTurnDriver``'s scan): no read. Best effort: a failure here is logged by type
        and never changes the student's interrupted turn."""
        site = bound_site()
        if site is None:
            log.warning("assess_provider_alert_failed", error_type="SiteUnbound")
            return
        try:
            async with self.engine.begin() as conn:
                await queue_provider_config_alert(conn, site, cause)
        except Exception as exc:
            log.warning("assess_provider_alert_failed", error_type=type(exc).__name__)

    async def _is_configuration(
        self, exc: ProviderOutageError, budget_s: float
    ) -> bool:
        """Whether this refusal is the deployment's configuration (``llm.probe``), with the
        runner's turn-shaped probe built only when needed and run within ``budget_s`` (the
        time left to the reply's cut, at most PROBE_MAX_S). Any failure is "not confirmed":
        the marked turn is written either way."""
        make = getattr(self.runner, "config_probe", None)
        try:
            return await refusal_is_configuration(
                exc, make if callable(make) else None, budget_s=budget_s
            )
        except Exception as probe_exc:
            log.warning(
                "assess_config_probe_failed", error_type=type(probe_exc).__name__
            )
            return False

    @staticmethod
    async def _enter_outage(
        conn: Any, sid: UUID, epoch: int, record: ProviderCallRecord, reason: str
    ) -> tuple[str | None, int | None]:
        """``assess_enter_outage`` in the caller's transaction: 'outage', 'closed' or 'noop',
        with, for 'outage', the session's outage count read in the same transaction (the broker
        compares it with its polls'). ``reason`` is the interval's ``reason_code``: the call
        outcome for a provider outage, ``service_misconfigured`` for a configuration refusal
        (read back by the snapshot and the stream's poll)."""
        got = (
            await conn.execute(
                _ENTER_OUTAGE,
                {
                    "sid": sid,
                    "epoch": epoch,
                    "started": record.started_at,
                    "reason": reason,
                },
            )
        ).scalar()
        if got != "outage":
            return (str(got) if got is not None else None), None
        count = await conn.execute(OUTAGE_COUNT, {"s": sid})
        return "outage", int(count.scalar_one())

    def _observe_outage(
        self, sid: UUID, got: str | None, reason: str, count: int | None = None
    ) -> None:
        """Tell the stream what ``assess_enter_outage`` did: ``outage_start`` (with the closed
        student-facing reason) or, at the third consecutive failure, ``closed``."""
        if got == "outage":
            self.broker.observe(
                sid, "OUTAGE", {"state": "OUTAGE", "reason": reason}, outage_count=count
            )
        elif got == "closed":
            # the third consecutive failed call: closed service_failure (M4.5)
            self.cache.evict(sid)
            self.broker.observe(
                sid,
                "CLOSED",
                {
                    "state": "CLOSED",
                    "close_reason": "service_failure",
                    "message_code": close_message_code("service_failure"),
                },
            )

    async def _outage(self, sid: UUID, epoch: int, record: ProviderCallRecord) -> None:
        """ACTIVE -> OUTAGE (M4.1): the pending turn is kept for the ``PendingTurnDriver``. The
        stream learns it from ``observe`` here (``outage_start``) or from its own poll."""
        async with self.engine.begin() as conn:
            got, count = await self._enter_outage(
                conn, sid, epoch, record, record.outcome
            )
        if got in ("outage", "closed"):
            self._observe_outage(sid, got, PROVIDER_UNAVAILABLE, count)
        else:
            # not ACTIVE at this epoch any more (closed, revoked): nothing is moved; the marked
            # turn is attempted and refused by the database when the session is gone
            await self._interrupted(sid, epoch, PROVIDER_UNAVAILABLE)

    async def _append_bot(
        self,
        sid: UUID,
        epoch: int,
        kind: str,
        text_: str,
        tokens: int = 0,
        used: list[int] | None = None,
        notes: TurnNotes | None = None,
        after: Callable[[Any], Awaitable[None]] | None = None,
    ) -> int | None:
        """Append one bot turn under the row lock; None when the session is not ACTIVE at this
        epoch or the function refuses (seq or chain moved, state changed). ``tokens`` (the
        answered turn's TurnUsage) is added to the session budget in the same transaction; the
        new total is appended to ``used``. ``notes`` (post-check marks, contest entries, reply
        language) go to ``assess_turn_notes`` in the SAME transaction (plan 10): the transcript
        marks them, and the turn row itself is never updated. ``after`` runs in the SAME
        transaction once the turn is appended (never when it was refused)."""
        async with self.engine.begin() as conn:
            seq = (
                await conn.execute(
                    text(
                        "SELECT seq FROM assess_sessions WHERE id = :s AND state = 'ACTIVE' "
                        "AND epoch = :e FOR UPDATE"
                    ),
                    {"s": sid, "e": epoch},
                )
            ).scalar()
            if seq is None:
                return None
            material = await load_material(
                conn, sid, epoch, self.handoff_private, self.cache
            )
            turn_no = await append_turn(
                conn,
                material,
                epoch=epoch,
                seq=int(seq),
                role="bot",
                kind=kind,
                text_=text_,
            )
            if turn_no is not None and notes is not None:
                await conn.execute(
                    _NOTE,
                    {
                        "s": sid,
                        "n": turn_no,
                        "m": list(notes.marks),
                        "c": json.dumps(
                            [
                                {"label": c.label, "status": c.status}
                                for c in notes.contests
                            ]
                        ),
                        "l": notes.lang,
                    },
                )
            if turn_no is not None and tokens > 0:
                total = (
                    await conn.execute(_ADD_TOKENS, {"s": sid, "n": tokens})
                ).scalar_one()
                if used is not None:
                    used.append(int(total))
            if turn_no is not None and after is not None:
                await after(conn)
            return turn_no

    async def _interrupted(
        self,
        sid: UUID,
        epoch: int,
        reason: str,
        *,
        pause: ProviderCallRecord | None = None,
    ) -> bool:
        """The marked "[risposta interrotta]" bot turn: it answers the pending student turn, so
        ``pending_reply_since`` is cleared and the student can write again (REQ-070). Returns
        whether the database appended it.

        ``pause``: the refused call of a configuration refusal. The session enters OUTAGE
        ``service_misconfigured`` in the marker's own transaction (so the student is never
        free to write between the two), and only when the marker was appended."""
        entered: list[tuple[str | None, int | None]] = []

        async def enter(conn: Any) -> None:
            assert pause is not None
            entered.append(
                await self._enter_outage(conn, sid, epoch, pause, SERVICE_MISCONFIGURED)
            )

        turn_no = await self._append_bot(
            sid,
            epoch,
            "interrupted",
            INTERRUPTED_REPLY,
            after=enter if pause is not None else None,
        )
        data: dict[str, Any] = {"reason": reason}
        if turn_no is not None:
            data["turn_no"] = turn_no
        self.broker.publish(sid, "reply_interrupted", data)
        if entered:
            self._observe_outage(
                sid, entered[0][0], SERVICE_MISCONFIGURED, entered[0][1]
            )
        return turn_no is not None

    async def _persist(self, sid: UUID, epoch: int, result: TurnResult) -> None:
        kind = "interrupted" if result.interrupted else "normal"
        text_ = result.text
        if result.interrupted:
            # cut at deadline + grace (REQ-070): what was released, then the fixed marker
            text_ = f"{text_.rstrip()} {INTERRUPTED_REPLY}".strip()
        tokens = result.usage.prompt_tokens + result.usage.completion_tokens
        used: list[int] = []
        notes = turn_notes(result)
        turn_no = await self._append_bot(sid, epoch, kind, text_, tokens, used, notes)
        if turn_no is None:
            # refused by assess_append_turn (state, epoch, seq or chain moved; from plan M3.3 also
            # past deadline + grace, by the database's own clock, which is left to decide): the
            # marked turn is attempted once, and is refused too when the database says no
            await self._interrupted(sid, epoch, "append_refused")
            return
        self.broker.publish(
            sid,
            "reply_interrupted" if result.interrupted else "reply_done",
            {"turn_no": turn_no, "marked": bool(result.marks)},
        )
        if used:
            await self._budget(sid, epoch, used[0] - tokens, used[0])

    async def _budget(self, sid: UUID, epoch: int, before: int, after: int) -> None:
        """REQ-083: ``budget_warning`` once, when this turn crosses 80%; at 100% the session is
        closed ``budget_exhausted`` through the plan-2 close function (one transcript job)."""
        budget = self.token_budget
        if not budget:
            return
        if before < 0.8 * budget <= after:
            async with self.engine.begin() as conn:
                await write_event(
                    conn,
                    AssessAuditEvent.BUDGET_WARNING,
                    actor="api",
                    session_id=sid,
                    data={"percent": 80},
                )
            self.broker.publish(sid, "budget_warning", {"percent": 80})
        if after >= budget:
            async with self.engine.begin() as conn:
                closed = (
                    await conn.execute(_CLOSE_BUDGET, {"sid": sid, "epoch": epoch})
                ).scalar()
            if closed:
                self.cache.evict(sid)
                self.broker.observe(
                    sid,
                    "CLOSED",
                    {
                        "state": "CLOSED",
                        "close_reason": "budget_exhausted",
                        "message_code": close_message_code("budget_exhausted"),
                    },
                )


def driver_of(request: Request) -> TurnDriver:
    driver = getattr(request.app.state, "assess_turn_driver", None)
    if driver is not None:
        return driver  # type: ignore[no-any-return]
    runner = getattr(request.app.state, "assess_runner", None)
    if runner is None:
        raise fail(assess_unavailable("discussion_runner"))
    driver = TurnDriver(
        engine=engine_of(request),
        broker=broker_of(request),
        runner=runner,
        handoff_private=handoff_private_of(request),
        cache=dek_cache_of(request),
        token_budget=settings_of(request).session_token_budget,
    )
    request.app.state.assess_turn_driver = driver
    return driver


class _AppRequest:
    """The app-level state accessors take a request; a background task has only the app."""

    def __init__(self, app: Any) -> None:
        self.app = app


def driver_for_app(app: Any) -> TurnDriver:
    return driver_of(_AppRequest(app))  # type: ignore[arg-type]


def cut_after_s(row: SessionRow) -> float | None:
    """Seconds until deadline + grace, minus the persist margin (REQ-070)."""
    if row.deadline_at is None:
        return None
    grace = row.reply_grace_s
    until = (row.deadline_at - row.server_now).total_seconds() + grace
    return until - min(CUT_MARGIN_S, grace / 2)


@router.post("/sessions/{sid}/turns", status_code=202)
async def post_turn(
    sid: UUID,
    request: Request,
    claims: SessionClaims = Depends(verify_assess_token_session),
) -> JSONResponse:
    body = await parse_body(request, TurnRequest)
    # REQ-083 pre-LLM checks: a blank or over-length message is a 400 envelope (never the 422 of
    # DEBT-035), before the gate, the material and any model call
    if not body.text.strip():
        raise fail(bad_request("message_empty"))
    if len(body.text) > settings_of(request).max_message_chars:
        raise fail(bad_request("message_too_long"))
    driver = driver_of(request)
    async with (
        session_tx(
            request,
            claims,
            sid,
            route="post_turn",
            allowed={"ACTIVE"},
            require_enrolment=True,  # gate step 6 (M3.7): a turn needs a fresh 'enrolled' check
        ) as (conn, row)
    ):
        # REQ-070 input lock from the deadline on (the database refuses the append as well)
        if row.deadline_at is not None and row.server_now >= row.deadline_at:
            raise fail(deadline_passed())
        # one reply at a time, decided on the locked row BEFORE the rate (review fix 4): a
        # message refused as pending never uses the student's messages-per-minute budget
        if row.pending_reply_since is not None:
            raise fail(reply_pending())
        # REQ-083 messages per minute per session: counted only for requests that passed the
        # gate (a stale or foreign token cannot use up the student's rate), before any model call
        allowed, _headers = limiter_of(request, "turns").check(
            str(sid), settings_of(request).messages_per_min
        )
        if not allowed:
            raise fail(rate_limited(60, "message_rate"))
        material = await load_material(
            conn, sid, row.epoch, handoff_private_of(request), dek_cache_of(request)
        )
        if material.turns and material.turns[-1].role == "student":
            raise fail(reply_pending())
        turn_no = await append_turn(
            conn,
            material,
            epoch=row.epoch,
            seq=row.seq,
            role="student",
            kind="normal",
            text_=body.text,
        )
        if turn_no is None:
            raise fail(state_conflict(row.state))
        bundle = bundle_of(material, row)
        turn = StudentTurn(
            text=body.text,
            turn_index=sum(1 for t in material.turns if t.role == "student") + 1,
            history=history_of(material),
        )
    # committed: the reply runs detached from this request from here on
    driver.broker.publish(sid, "turn_accepted", {"turn_no": turn_no})
    # stop early enough to persist the cut turn before the database's own limit
    # row.seq is the seq the student turn was appended at; the append moved it by one
    driver.start(
        sid, row.epoch, bundle, turn, cut_after_s(row), expected_seq=row.seq + 1
    )
    return JSONResponse(
        status_code=202,
        content={"session_id": str(sid), "turn_no": turn_no},
        headers={"Cache-Control": "no-store"},
    )
