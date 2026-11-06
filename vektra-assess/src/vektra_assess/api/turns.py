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

Thin path (plan M1.9): a provider outage, an unexpected error or a refused bot append ends the
attempt with the marked bot turn "[risposta interrotta]" (``turn_markers``), which clears the
pending marker, then ``reply_interrupted``. The OUTAGE transition, the server-side answer of a
pending turn and the reconciliation of an abandoned one are plans M4.1, M4.3 and M3.6.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
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
from vektra_assess.api.events import EventBroker, broker_of
from vektra_assess.api.gate import verify_assess_token_session
from vektra_assess.api.material import DekCache, Material, load_material
from vektra_assess.api.models import TurnRequest
from vektra_assess.api.session_gate import SessionRow, session_tx
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import FrozenPolicy
from vektra_assess.context.snapshot import from_canonical_bytes
from vektra_assess.crypto import envelope, hashing
from vektra_assess.errors import (
    assess_unavailable,
    bad_request,
    deadline_passed,
    reply_pending,
    state_conflict,
)
from vektra_assess.keys import AssessKeyring
from vektra_assess.llm.types import (
    PriorTurn,
    ProviderOutageError,
    ReleasedSegment,
    StudentTurn,
    TurnResult,
)
from vektra_assess.tokens import SessionClaims
from vektra_assess.turn_markers import INTERRUPTED_REPLY

log = structlog.get_logger(__name__)

router = APIRouter()

#: seconds kept before deadline + grace to persist a cut reply
CUT_MARGIN_S = 2.0

_APPEND = text(
    "SELECT assess_append_turn(:sid, :epoch, :seq, :role, :kind, :ct, :h, :prev)"
)
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
    tasks: dict[UUID, asyncio.Task[None]] = field(default_factory=dict)

    def start(
        self,
        sid: UUID,
        epoch: int,
        bundle: ContextBundle,
        turn: StudentTurn,
        cut_after_s: float | None = None,
    ) -> asyncio.Task[None]:
        """``cut_after_s``: seconds until deadline + grace (minus the persist margin): the reply
        is stopped then and the released part is persisted as a marked interrupted turn."""
        task = asyncio.create_task(self._drive(sid, epoch, bundle, turn, cut_after_s))
        self.tasks[sid] = task
        task.add_done_callback(
            lambda t: self.tasks.pop(sid, None) if self.tasks.get(sid) is t else None
        )
        return task

    async def _drive(
        self,
        sid: UUID,
        epoch: int,
        bundle: ContextBundle,
        turn: StudentTurn,
        cut_after_s: float | None = None,
    ) -> None:
        result: TurnResult | None = None
        failure: str | None = None
        stop = asyncio.Event()
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
        except ProviderOutageError:
            # the ACTIVE -> OUTAGE transition and the server-side answer are plan M4.1 / M4.3;
            # until then the attempt ends with the marked interrupted turn
            failure = "provider_unavailable"
        except Exception as exc:
            log.warning("assess_turn_driver_error", error_type=type(exc).__name__)
            failure = "internal_error"
        finally:
            if cut is not None:
                cut.cancel()
        if failure is None and result is None:
            failure = "internal_error"
        try:
            if failure is not None or result is None:
                await self._interrupted(sid, epoch, failure or "internal_error")
            else:
                await self._persist(sid, epoch, result)
        except Exception as exc:
            # a lost hand-off, an unseal or a database failure while persisting: reported to the
            # stream and logged by type only; the task never ends with an unread exception
            log.warning("assess_turn_persist_failed", error_type=type(exc).__name__)
            self.broker.publish(sid, "reply_interrupted", {"reason": "persist_failed"})

    async def _append_bot(
        self, sid: UUID, epoch: int, kind: str, text_: str
    ) -> int | None:
        """Append one bot turn under the row lock; None when the session is not ACTIVE at this
        epoch or the function refuses (seq or chain moved, state changed)."""
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
            return await append_turn(
                conn,
                material,
                epoch=epoch,
                seq=int(seq),
                role="bot",
                kind=kind,
                text_=text_,
            )

    async def _interrupted(self, sid: UUID, epoch: int, reason: str) -> None:
        """The marked "[risposta interrotta]" bot turn: it answers the pending student turn, so
        ``pending_reply_since`` is cleared and the student can write again (REQ-070)."""
        turn_no = await self._append_bot(sid, epoch, "interrupted", INTERRUPTED_REPLY)
        data: dict[str, Any] = {"reason": reason}
        if turn_no is not None:
            data["turn_no"] = turn_no
        self.broker.publish(sid, "reply_interrupted", data)

    async def _persist(self, sid: UUID, epoch: int, result: TurnResult) -> None:
        kind = "interrupted" if result.interrupted else "normal"
        text_ = result.text
        if result.interrupted:
            # cut at deadline + grace (REQ-070): what was released, then the fixed marker
            text_ = f"{text_.rstrip()} {INTERRUPTED_REPLY}".strip()
        turn_no = await self._append_bot(sid, epoch, kind, text_)
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
    )
    request.app.state.assess_turn_driver = driver
    return driver


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
    async with session_tx(
        request, claims, sid, route="post_turn", allowed={"ACTIVE"}
    ) as (conn, row):
        # REQ-070 input lock from the deadline on (the database refuses the append as well)
        if row.deadline_at is not None and row.server_now >= row.deadline_at:
            raise fail(deadline_passed())
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
    cut_after = None
    if row.deadline_at is not None:
        grace = row.reply_grace_s
        until = (row.deadline_at - row.server_now).total_seconds() + grace
        # stop early enough to persist the cut turn before the database's own limit
        cut_after = until - min(CUT_MARGIN_S, grace / 2)
    driver.start(sid, row.epoch, bundle, turn, cut_after)
    return JSONResponse(
        status_code=202,
        content={"session_id": str(sid), "turn_no": turn_no},
        headers={"Cache-Control": "no-store"},
    )
