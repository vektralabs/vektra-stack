"""``DiscussionTurnRunner``: the only code path that sends discussion content to a model (COMP-006).

Not a QueryPipeline and not in the ProviderRegistry (ARCH-074): the composition roots in vektra_app
inject the LLM provider, the safeguard, the trace writer and the audit sink, so this module imports
no other component. Its only input type is ``ContextBundle`` (ARCH-069).

RAG is OFF in v1 and there is no retrieval branch here at all: a bundle frozen with
``course_material`` set is refused with ``RagNotAvailableError`` (NFR-015), never answered without
the material the session was promised.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.part_a import fmt_date, grade_text
from vektra_assess.context.references import canonical_labels
from vektra_assess.llm import shipped
from vektra_assess.llm.budget import MESSAGE_OVERHEAD, fit
from vektra_assess.llm.language import detect
from vektra_assess.llm.postcheck import (
    citations,
    feedback_quote,
    lexicon,
    markers,
    part_b,
)
from vektra_assess.llm.postcheck.gate import MARK_SAFEGUARD, SentenceGate, Unit
from vektra_assess.llm.postcheck.lexicon import LEXICON_VERSION
from vektra_assess.llm.postcheck.sentences import split
from vektra_assess.llm.prompts import PromptRenderer
from vektra_assess.llm.shipped import Shipped
from vektra_assess.llm.types import (
    AuditSink,
    CallOutcome,
    PartBRejectedError,
    PriorTurn,
    ProviderCallRecord,
    ProviderOutageError,
    RagNotAvailableError,
    ReleasedSegment,
    RunnerMetrics,
    StudentTurn,
    TraceWriter,
    TurnResult,
    TurnUsage,
)
from vektra_shared.protocols import LLMProvider, SafeguardHook
from vektra_shared.types import (
    CompletionChunk,
    Message,
    QueryTrace,
    SafeguardContext,
    StepTrace,
)

PIPELINE = "assess-discussion"
ASSESS_NAMESPACE = "assess-discussion"  # reserved namespace (plan task C migration)
KEY_SCOPE = "assess:session"

log = structlog.get_logger(__name__)


class DiscussionTurnRunner:
    def __init__(
        self,
        llm: LLMProvider,
        safeguard: SafeguardHook,
        trace_writer: TraceWriter,
        settings: VektraAssessSettings,
        audit: AuditSink,
        renderer: PromptRenderer | None = None,
        metrics: RunnerMetrics | None = None,
    ) -> None:
        if not settings.llm_model:
            raise ValueError("VEKTRA_ASSESS_LLM_MODEL is not set")
        self._llm = llm
        self._safeguard = safeguard
        self._trace_writer = trace_writer
        self._settings = settings
        self._audit = audit
        self._renderer = renderer or PromptRenderer(LEXICON_VERSION)
        self._metrics = metrics

    @property
    def model(self) -> str:
        return str(self._settings.llm_model)

    @property
    def prompt_version(self) -> str:
        return self._renderer.prompt_version

    def _count(self, text: str) -> int:
        return self._llm.count_tokens(text, self.model) if text else 0

    def _usage(self, prompt_tokens: int, completion_tokens: int) -> TurnUsage:
        """Estimated tokens (the stream reports none) and, with an operator price table, cost.

        On-prem models have no price: ``cost`` stays None rather than a misleading 0.
        """
        p_in, p_out = (
            self._settings.llm_price_per_1k_in,
            self._settings.llm_price_per_1k_out,
        )
        cost = None
        if p_in is not None or p_out is not None:
            cost = round(
                prompt_tokens / 1000 * (p_in or 0.0)
                + completion_tokens / 1000 * (p_out or 0.0),
                6,
            )
        return TurnUsage(prompt_tokens, completion_tokens, estimated=True, cost=cost)

    async def _write_trace(
        self, response_id: UUID, steps: _Steps, t_all: float
    ) -> QueryTrace | None:
        """Build the content-free trace and hand it to the writer; a writer failure never fails
        the turn (the student already has the reply), it is audited instead."""
        if not self._settings.store_traces:
            return None
        trace = QueryTrace(
            response_id=response_id,
            steps=steps.items,
            total_duration_ms=int((time.monotonic() - t_all) * 1000),
            chunks_retrieved=[],  # RAG is OFF in v1
            llm_model=self.model,
            prompt_version=self.prompt_version,
            created_at=_now(),
            pipeline=PIPELINE,
        )
        try:
            await self._trace_writer.write(trace, ASSESS_NAMESPACE)
        except Exception as exc:
            try:
                await self._audit(
                    "assess_trace_write_failed", {"error_type": type(exc).__name__}
                )
            except Exception as audit_exc:
                # neither failure may fail the turn: a content-free log line is the last resort
                log.warning(
                    "assess_trace_write_failed",
                    error_type=type(exc).__name__,
                    audit_error_type=type(audit_exc).__name__,
                )
        return trace

    async def _finish_failed(
        self, gate: SentenceGate, turn: StudentTurn, steps: _Steps, t_all: float
    ) -> None:
        """A turn that ends in an outage after the post-check already ran: its buffered audits
        and its content-free trace are still written before the outage propagates."""
        for event, data in gate.audit:
            await self._audit(event, data)
        await self._write_trace(turn.turn_id, steps, t_all)

    async def _student_history(
        self, turn: StudentTurn, sh: Shipped
    ) -> tuple[tuple[PriorTurn, ...], bool]:
        """The history with every earlier STUDENT message through ``pre_query`` too, so the
        original PII of a past message never reaches the prompt; delivered assistant text is
        already post-checked. Returns the history and whether any message was changed."""
        out: list[PriorTurn] = []
        changed = False
        for prior in turn.history:
            if prior.role != "student":
                out.append(prior)
                continue
            res = await self._safeguard.pre_query(
                prior.text, self._context(turn.turn_index, history=True)
            )
            if not res.allowed:
                text = sh.missing["replaced_by_system"]
            elif res.modified_content is not None:
                text = res.modified_content
            else:
                text = prior.text
            changed = changed or text != prior.text
            out.append(PriorTurn(prior.role, text))
        return tuple(out), changed

    def _outage(self, rec: ProviderCallRecord) -> ProviderOutageError:
        if self._metrics is not None:
            self._metrics.call_outcome(rec.outcome)
        return ProviderOutageError(rec)

    def _emit_turn(self, gate: SentenceGate, records: list[ProviderCallRecord]) -> None:
        if self._metrics is None:
            return
        for stage, n in gate.by_stage.items():
            self._metrics.replacement(stage, n)
        if gate.behaviour_class is not None:
            self._metrics.behaviour_class(gate.behaviour_class)
        for rec in records:
            self._metrics.call_outcome(rec.outcome)

    @staticmethod
    def _admit(bundle: ContextBundle) -> None:
        """Fail closed on anything v1 cannot honour before a byte is rendered."""
        if bundle.course_material:
            raise RagNotAvailableError("course material (RAG) is not available in v1")

    # --- turns --------------------------------------------------------------------------------

    def _context(
        self, turn_index: int, *, partial: bool | None = None, history: bool = False
    ) -> SafeguardContext:
        meta: dict[str, object] = {"turn_index": turn_index, "pipeline": PIPELINE}
        if partial is not None:
            meta["partial"] = partial
        if history:
            meta["history"] = True
        # never the "admin" default: the assess turn has its own scope
        return SafeguardContext(
            namespace=ASSESS_NAMESPACE, key_scope=KEY_SCOPE, metadata=meta
        )

    def _br009(self, bundle: ContextBundle, sh: Shipped) -> str:
        return sh.br009.format(
            grade=grade_text(bundle), date=fmt_date(bundle.overall.grade_read_at)
        )

    async def _open_stream(
        self,
        messages: list[Message],
        records: list[ProviderCallRecord],
        stop: asyncio.Event | None = None,
    ) -> tuple[AsyncIterator[CompletionChunk], CompletionChunk, datetime] | None:
        """Open the provider stream and wait for its first non-empty chunk.

        ONE outage deadline covers both the open and the first token (an empty role chunk does
        not reset it), and a stream that ends before any text is an outage, never an empty
        reply. ``None`` means the caller's ``stop`` won first: the stream is already closed and
        a ``cancelled`` record appended.
        """
        s = self._settings
        kwargs: dict[str, object] = {}
        if s.llm_seed is not None:
            kwargs["seed"] = s.llm_seed
        started = _now()
        holder: list[AsyncIterator[CompletionChunk]] = []

        async def acquire() -> CompletionChunk | None:
            stream = await self._llm.stream(
                messages,
                self.model,
                temperature=s.llm_temperature,
                max_tokens=s.llm_max_tokens,
                **kwargs,
            )
            holder.append(stream.__aiter__())
            while True:
                chunk = await anext(holder[0], None)
                if chunk is None or chunk.content:
                    return chunk

        async def give_up(outcome: CallOutcome, error: str | None = None) -> None:
            if holder:
                await _close(holder[0])
            records.append(ProviderCallRecord(started, None, _now(), outcome, error))

        if stop is not None and stop.is_set():
            await give_up("cancelled")
            return None
        task = asyncio.ensure_future(acquire())
        stopper = asyncio.ensure_future(stop.wait()) if stop is not None else None
        waits: set[asyncio.Future[Any]] = {task} | ({stopper} if stopper else set())
        try:
            done, _ = await asyncio.wait(
                waits, timeout=s.outage_timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            await _cancel(task)
            await give_up("cancelled")
            raise
        finally:
            if stopper is not None:
                stopper.cancel()
        if task not in done:
            await _cancel(task)
            if stopper is not None and stopper in done:
                await give_up("cancelled")
                return None
            await give_up("first_token_timeout")
            raise self._outage(records[-1]) from None
        try:
            first = task.result()
        # any provider failure is an outage record, never its text
        except Exception as exc:
            await give_up("error", type(exc).__name__)
            raise self._outage(records[-1]) from None
        if first is None:
            await give_up("error", "EmptyStream")
            raise self._outage(records[-1]) from None
        return holder[0], first, started

    async def _release(
        self, unit: Unit, gate: SentenceGate, turn_index: int
    ) -> ReleasedSegment:
        """One unit through the response safeguard. Vektra-shipped text (a refusal core sentence,
        the BR-009 notice with its date) is not model output and is released as shipped. Model
        text the safeguard anonymises is released anonymised (ARCH-049) and marked; text it
        disallows is replaced as a whole."""
        if unit.system:
            return ReleasedSegment(unit.text, unit.marked)
        res = await self._safeguard.pre_response(
            unit.text, self._context(turn_index, partial=True)
        )
        if not res.allowed:
            gate._mark(MARK_SAFEGUARD)
            gate._drop_contest(unit.contest)  # the student never sees that sentence
            return ReleasedSegment(gate.shipped.missing["replaced_by_system"], True)
        gate.commit(unit)
        if res.modified_content is not None:
            gate._mark(MARK_SAFEGUARD)
            return ReleasedSegment(res.modified_content, True)
        return ReleasedSegment(unit.text, unit.marked)

    async def run_stream(
        self,
        bundle: ContextBundle,
        turn: StudentTurn,
        stop: asyncio.Event | None = None,
    ) -> AsyncIterator[ReleasedSegment | TurnResult]:
        """Yield each released segment as soon as its sentence passed every check, then one
        ``TurnResult``. Nothing reaches the caller before the post-check (release discipline).

        ``stop`` is the caller's cancellation (client gone, deadline plus grace): once set, the
        provider stream is closed, the unfinished sentence is dropped and the ``TurnResult`` has
        ``interrupted=True`` and only the text already released.
        """
        self._admit(bundle)
        detected = detect(turn.text)
        language = detected if detected in ("it", "en") else "it"
        sh = shipped.get(language)
        gate = SentenceGate(bundle, sh, self._br009(bundle, sh))
        released: list[ReleasedSegment] = []
        records: list[ProviderCallRecord] = []
        prompt_tokens = completion_tokens = 0
        completion = ""
        interrupted = False
        steps = _Steps()
        t_all = time.monotonic()

        t = time.monotonic()
        pre = await self._safeguard.pre_query(turn.text, self._context(turn.turn_index))
        steps.add(
            "safeguard_pre_query",
            t,
            allowed=pre.allowed,
            modified=pre.modified_content is not None,
        )
        if not pre.allowed:
            # no model call at all; the student sees the fixed replacement, the audit says why
            await self._audit(
                "assess_prequery_refused", {"turn_index": turn.turn_index}
            )
            gate._mark(MARK_SAFEGUARD)
            seg = ReleasedSegment(sh.missing["replaced_by_system"], True)
            released.append(seg)
            yield seg
        else:
            if detected == "other":
                seg = ReleasedSegment(sh.unsupported_language)
                released.append(seg)
                yield seg
            # anonymised text, if any (an empty rewrite is a rewrite, not "no change")
            student_text = (
                pre.modified_content if pre.modified_content is not None else turn.text
            )
            t = time.monotonic()
            history, history_changed = await self._student_history(turn, sh)
            head, past, tail = self._renderer.turn_parts(
                bundle, history, student_text, language
            )
            prompt = fit(
                head,
                past,
                tail,
                count=lambda t: self._llm.count_tokens(t, self.model),
                context_window=self._settings.llm_context_window,
                max_tokens=self._settings.llm_max_tokens,
            )
            prompt_tokens = prompt.prompt_tokens
            steps.add(
                "build_prompt",
                t,
                prompt_tokens=prompt.prompt_tokens,
                history_kept=prompt.history_kept,
                history_dropped=prompt.history_dropped,
                history_anonymised=history_changed,
            )
            t_llm = time.monotonic()
            opened = await self._open_stream(prompt.messages, records, stop)
            if opened is None:  # stopped before the first token: nothing was released
                interrupted = True
                steps.add("llm_call", t_llm, completion_tokens=0, outcome="cancelled")
            else:
                it, first, started = opened
                first_at = _now()
                first_token_ms = int((time.monotonic() - t_llm) * 1000)
                buf = ""
                outcome: CallOutcome = "ok"
                waiter = _StopWaiter(stop)
                chunk: CompletionChunk | object | None = first
                try:
                    while True:
                        if chunk is None:
                            break
                        assert isinstance(chunk, CompletionChunk)
                        completion += chunk.content
                        buf += chunk.content
                        done, buf = split(buf)
                        for sentence in done:
                            for unit in gate.feed(sentence):
                                if stop is not None and stop.is_set():
                                    break
                                seg = await self._release(unit, gate, turn.turn_index)
                                released.append(seg)
                                yield seg
                        if stop is not None and stop.is_set():
                            outcome = "cancelled"
                            break
                        try:
                            # only the provider's own failure is an outage: a bug in the gate
                            # or the safeguard propagates as itself (REQ-086 must not fire); a
                            # stream that stalls is bounded by the outage timeout
                            chunk = await waiter.next(
                                it, timeout=self._settings.outage_timeout_s
                            )
                        except Exception as exc:
                            stall = isinstance(exc, TimeoutError)
                            rec = ProviderCallRecord(
                                started,
                                first_at,
                                _now(),
                                "error",
                                "StreamStall" if stall else type(exc).__name__,
                            )
                            records.append(rec)
                            await self._finish_failed(gate, turn, steps, t_all)
                            raise self._outage(rec) from None
                        if chunk is _STOPPED:
                            outcome = "cancelled"
                            break
                finally:
                    waiter.close()
                    await _close(it)  # the provider stream is closed on every path
                records.append(ProviderCallRecord(started, first_at, _now(), outcome))
                steps.add(
                    "llm_call",
                    t_llm,
                    first_token_ms=first_token_ms,
                    completion_tokens=(completion_tokens := self._count(completion)),
                    outcome=outcome,
                )
                if outcome == "cancelled":
                    interrupted = (
                        True  # the unfinished sentence in the buffer is dropped
                    )
                else:
                    done, _ = split(buf, final=True)
                    for sentence in done:
                        for unit in gate.feed(sentence):
                            seg = await self._release(unit, gate, turn.turn_index)
                            released.append(seg)
                            yield seg

        delivered = " ".join(s.text for s in released)
        steps.add_measured(
            "postcheck",
            gate_ms=int(gate.elapsed_ms),
            replacements=dict(gate.by_stage),
            behaviour_class=gate.behaviour_class,
            language=language,
            released=len(released),
        )
        # the whole turn once more, for the safeguard's audit only: nothing is retracted
        t = time.monotonic()
        await self._safeguard.pre_response(
            delivered, self._context(turn.turn_index, partial=False)
        )
        steps.add(
            "safeguard_pre_response",
            t,
            sentences=len(released),
            replaced=gate.by_stage.get(MARK_SAFEGUARD, 0),
        )
        for event, data in gate.audit:
            await self._audit(event, data)
        trace = await self._write_trace(turn.turn_id, steps, t_all)
        self._emit_turn(gate, records)
        yield TurnResult(
            text=delivered,
            marks=tuple(gate.marks),
            behaviour_class=gate.behaviour_class,
            contest_entries=tuple(gate.contest),
            language=language,
            usage=self._usage(prompt_tokens, completion_tokens),
            call_records=tuple(records),
            replaced_count=gate.replaced,
            content_hash=hashlib.sha256(delivered.encode("utf-8")).hexdigest(),
            interrupted=interrupted,
            trace=trace,
        )

    async def _part_b_once(
        self, bundle: ContextBundle, language: str, records: list[ProviderCallRecord]
    ) -> tuple[str, tuple[str, ...], str]:
        """One generation: (checked text, violation codes, raw completion)."""
        messages = self._renderer.part_b_messages(bundle, language)
        # the same budget as a turn: an oversized prompt fails before generation, not inside it
        messages = fit(
            messages,
            [],
            [],
            count=lambda t: self._llm.count_tokens(t, self.model),
            context_window=self._settings.llm_context_window,
            max_tokens=self._settings.llm_max_tokens,
        ).messages
        opened = await self._open_stream(messages, records)
        assert opened is not None  # no stop event: only an outage ends it early
        it, first, started = opened
        first_at = _now()
        raw = first.content
        waiter = _StopWaiter(None)
        try:
            while True:
                try:
                    # Part B has no caller stop: a stream that stalls after its first token is
                    # bounded by the outage timeout, so the pre-flight never hangs on it
                    chunk = await waiter.next(
                        it, timeout=self._settings.outage_timeout_s
                    )
                except TimeoutError:
                    rec = ProviderCallRecord(
                        started, first_at, _now(), "error", "StreamStall"
                    )
                    records.append(rec)
                    raise self._outage(rec) from None
                except Exception as exc:
                    rec = ProviderCallRecord(
                        started, first_at, _now(), "error", type(exc).__name__
                    )
                    records.append(rec)
                    raise self._outage(rec) from None
                if chunk is None:
                    break
                assert isinstance(chunk, CompletionChunk)
                raw += chunk.content
                if part_b.word_count(raw) > part_b.MAX_WORDS:
                    break  # the stream stops at the cap; the check below rejects it
        finally:
            waiter.close()
            await _close(it)
        records.append(ProviderCallRecord(started, first_at, _now(), "ok"))
        violations: list[str] = []
        m = markers.extract(raw, canonical_labels(bundle))
        if m.behaviour_class or m.contest or m.dropped:
            violations.append(part_b.MARKER)
        text = " ".join(m.text.split())
        c = citations.check(text, canonical_labels(bundle))
        if c.removed:
            violations.append(part_b.UNRESOLVED)
        if lexicon.scan(text) is not None:
            violations.append(part_b.LEXICON)
        if feedback_quote.check(text, feedback_quote.feedback_index(bundle)).replaced:
            violations.append(part_b.QUOTE)
        violations += part_b.check(text).violations
        res = await self._safeguard.pre_response(text, self._context(-1, partial=False))
        if not res.allowed or res.modified_content is not None:
            violations.append(part_b.SAFEGUARD)
        return text, tuple(violations), raw

    async def run_part_b(
        self,
        bundle: ContextBundle,
        *,
        language: str = "it",
        remaining_s: float | None = None,
    ) -> TurnResult:
        """The LLM orientation of the opening message (REQ-094 Part B).

        One regeneration is attempted when the first one breaks a Part B rule and the caller's
        remaining time allows it; a second failure raises ``PartBRejectedError`` with a
        content-free code, and the caller (worker pre-flight) rolls the start back. Nothing is
        ever truncated into an accepted Part B.
        """
        self._admit(bundle)
        language = language if language in ("it", "en") else "it"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        records: list[ProviderCallRecord] = []
        codes: tuple[str, ...] = ()
        prompt_spent = completion_spent = 0
        # the Part B prompt is the same for every attempt: rendered and counted once
        part_b_prompt_tokens = sum(
            self._count(m.content) + MESSAGE_OVERHEAD
            for m in self._renderer.part_b_messages(bundle, language)
        )
        steps = _Steps()
        t_all = time.monotonic()
        part_b_id = uuid4()
        for attempt in (1, 2):
            if (
                attempt == 2
                and remaining_s is not None
                and loop.time() - t0 >= remaining_s
            ):
                break  # no time left for a second generation
            t_attempt = time.monotonic()
            text, codes, raw = await self._part_b_once(bundle, language, records)
            if self._metrics is not None:  # every completed call, accepted or rejected
                self._metrics.call_outcome(records[-1].outcome)
            steps.add(
                "llm_call",
                t_attempt,
                attempt=attempt,
                completion_tokens=self._count(raw),
                outcome=records[-1].outcome,
            )
            steps.add_measured(
                "postcheck", 0, mode="part_b", violations=list(codes), language=language
            )
            # every generation is paid for, the rejected one too (REQ-083 accounting)
            prompt_spent += part_b_prompt_tokens
            completion_spent += self._count(raw)
            if not codes:
                trace = await self._write_trace(part_b_id, steps, t_all)
                return TurnResult(
                    text=text,
                    marks=(),
                    behaviour_class=None,
                    contest_entries=(),
                    language=language,
                    usage=self._usage(prompt_spent, completion_spent),
                    call_records=tuple(records),
                    replaced_count=0,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    trace=trace,
                )
            await self._audit(
                "assess_part_b_rejected",
                {"attempt": attempt, "reason_code": ",".join(codes)},
            )
            if self._metrics is not None:
                for code in codes:
                    self._metrics.part_b_rejected(code)
        await self._write_trace(
            part_b_id, steps, t_all
        )  # a rejected Part B is traced too
        raise PartBRejectedError(codes[0] if codes else part_b.EMPTY)

    async def run(self, bundle: ContextBundle, turn: StudentTurn) -> TurnResult:
        """``run_stream`` consumed to the end."""
        result: TurnResult | None = None
        async for item in self.run_stream(bundle, turn):
            if isinstance(item, TurnResult):
                result = item
        assert result is not None  # run_stream always ends with one TurnResult
        return result


def _now() -> datetime:
    return datetime.now(UTC)


_STOPPED = object()


class _Steps:
    """Content-free StepTrace list: names, durations, counts and codes only."""

    def __init__(self) -> None:
        self.items: list[StepTrace] = []

    def add(self, name: str, started: float, **metadata: object) -> None:
        ms = int((time.monotonic() - started) * 1000)
        self.items.append(StepTrace(name=name, duration_ms=ms, metadata=dict(metadata)))

    def add_measured(self, name: str, gate_ms: int, **metadata: object) -> None:
        self.items.append(
            StepTrace(name=name, duration_ms=gate_ms, metadata=dict(metadata))
        )


class _StopWaiter:
    """Races each next chunk against the caller's stop event, with ONE waiter per stream."""

    def __init__(self, stop: asyncio.Event | None) -> None:
        self._stopped = asyncio.ensure_future(stop.wait()) if stop is not None else None

    async def next(
        self, it: AsyncIterator[CompletionChunk], timeout: float | None = None
    ) -> CompletionChunk | object | None:
        """The next chunk, ``None`` at the end, ``_STOPPED`` if stop is set first; a chunk that
        does not come within ``timeout`` raises ``TimeoutError``."""
        nxt = asyncio.ensure_future(anext(it, None))
        waits: set[asyncio.Future[Any]] = {nxt}
        if self._stopped is not None:
            waits.add(self._stopped)
        try:
            done, _ = await asyncio.wait(
                waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            await _cancel(nxt)  # the read stops before the caller closes the stream
            raise
        if nxt in done:
            return nxt.result()
        # let the cancelled read finish unwinding, so the stream can then really be closed
        # (closing an async generator that is still running raises and leaves it open)
        await _cancel(nxt)
        if self._stopped is not None and self._stopped in done:
            return _STOPPED
        raise TimeoutError("no chunk within the stall timeout")

    def close(self) -> None:
        if self._stopped is not None:
            self._stopped.cancel()


async def _cancel(task: asyncio.Future[Any]) -> None:
    """Cancel a pending read and wait until it has really stopped."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _close(it: AsyncIterator[CompletionChunk]) -> None:
    closer = getattr(it, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:
            pass
