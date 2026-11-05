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
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
from vektra_assess.llm.postcheck.gate import MARK_SAFEGUARD, SentenceGate
from vektra_assess.llm.postcheck.lexicon import LEXICON_VERSION
from vektra_assess.llm.postcheck.sentences import split
from vektra_assess.llm.prompts import PromptRenderer
from vektra_assess.llm.shipped import Shipped
from vektra_assess.llm.types import (
    AuditSink,
    CallOutcome,
    PartBRejectedError,
    ProviderCallRecord,
    ProviderOutageError,
    RagNotAvailableError,
    ReleasedSegment,
    StudentTurn,
    TraceWriter,
    TurnResult,
    TurnUsage,
)
from vektra_shared.protocols import LLMProvider, SafeguardHook
from vektra_shared.types import CompletionChunk, Message, SafeguardContext

PIPELINE = "assess-discussion"
ASSESS_NAMESPACE = "assess-discussion"  # reserved namespace (plan task C migration)
KEY_SCOPE = "assess:session"


class DiscussionTurnRunner:
    def __init__(
        self,
        llm: LLMProvider,
        safeguard: SafeguardHook,
        trace_writer: TraceWriter,
        settings: VektraAssessSettings,
        audit: AuditSink,
        renderer: PromptRenderer | None = None,
    ) -> None:
        if not settings.llm_model:
            raise ValueError("VEKTRA_ASSESS_LLM_MODEL is not set")
        self._llm = llm
        self._safeguard = safeguard
        self._trace_writer = trace_writer
        self._settings = settings
        self._audit = audit
        self._renderer = renderer or PromptRenderer(LEXICON_VERSION)

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

    @staticmethod
    def _admit(bundle: ContextBundle) -> None:
        """Fail closed on anything v1 cannot honour before a byte is rendered."""
        if bundle.course_material:
            raise RagNotAvailableError("course material (RAG) is not available in v1")

    # --- turns --------------------------------------------------------------------------------

    def _context(
        self, turn_index: int, *, partial: bool | None = None
    ) -> SafeguardContext:
        meta: dict[str, object] = {"turn_index": turn_index, "pipeline": PIPELINE}
        if partial is not None:
            meta["partial"] = partial
        # never the "admin" default: the assess turn has its own scope
        return SafeguardContext(
            namespace=ASSESS_NAMESPACE, key_scope=KEY_SCOPE, metadata=meta
        )

    def _br009(self, bundle: ContextBundle, sh: Shipped) -> str:
        return sh.br009.format(
            grade=grade_text(bundle), date=fmt_date(bundle.overall.grade_read_at)
        )

    async def _open_stream(
        self, messages: list[Message], records: list[ProviderCallRecord]
    ) -> tuple[AsyncIterator[CompletionChunk], CompletionChunk | None, datetime]:
        """Open the provider stream and wait for its first chunk within the outage timeout."""
        s = self._settings
        kwargs: dict[str, object] = {}
        if s.llm_seed is not None:
            kwargs["seed"] = s.llm_seed
        started = _now()
        try:
            stream = await self._llm.stream(
                messages,
                self.model,
                temperature=s.llm_temperature,
                max_tokens=s.llm_max_tokens,
                **kwargs,
            )
            it = stream.__aiter__()
            first = await asyncio.wait_for(anext(it, None), timeout=s.outage_timeout_s)
        except TimeoutError:
            rec = ProviderCallRecord(started, None, _now(), "first_token_timeout")
            records.append(rec)
            raise ProviderOutageError(rec) from None
        except (
            Exception
        ) as exc:  # any provider failure is an outage record, never its text
            rec = ProviderCallRecord(started, None, _now(), "error", type(exc).__name__)
            records.append(rec)
            raise ProviderOutageError(rec) from None
        return it, first, started

    async def _release(
        self, text: str, marked: bool, gate: SentenceGate, turn_index: int
    ) -> ReleasedSegment:
        res = await self._safeguard.pre_response(
            text, self._context(turn_index, partial=True)
        )
        if not res.allowed or res.modified_content is not None:
            gate._mark(MARK_SAFEGUARD)
            return ReleasedSegment(gate.shipped.missing["replaced_by_system"], True)
        return ReleasedSegment(text, marked)

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
        prompt_tokens = 0
        completion = ""
        interrupted = False

        pre = await self._safeguard.pre_query(turn.text, self._context(turn.turn_index))
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
            student_text = pre.modified_content or turn.text  # anonymised text, if any
            head, past, tail = self._renderer.turn_parts(
                bundle, turn.history, student_text, language
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
            it, first, started = await self._open_stream(prompt.messages, records)
            first_at = _now()
            buf = first.content if first is not None else ""
            completion = buf
            outcome: CallOutcome = "ok"
            try:
                while True:
                    if stop is not None and stop.is_set():
                        outcome = "cancelled"
                        break
                    chunk = await _next_chunk(it, stop)
                    if chunk is _STOPPED:
                        outcome = "cancelled"
                        break
                    if chunk is None:
                        break
                    assert isinstance(chunk, CompletionChunk)
                    completion += chunk.content
                    buf += chunk.content
                    done, buf = split(buf)
                    for sentence in done:
                        for text, marked in gate.feed(sentence):
                            seg = await self._release(
                                text, marked, gate, turn.turn_index
                            )
                            released.append(seg)
                            yield seg
            except Exception as exc:
                rec = ProviderCallRecord(
                    started, first_at, _now(), "error", type(exc).__name__
                )
                records.append(rec)
                raise ProviderOutageError(rec) from None
            finally:
                await _close(it)  # the provider stream is closed on every path
            records.append(ProviderCallRecord(started, first_at, _now(), outcome))
            if outcome == "cancelled":
                interrupted = True  # the unfinished sentence in the buffer is dropped
            else:
                done, _ = split(buf, final=True)
                for sentence in done:
                    for text, marked in gate.feed(sentence):
                        seg = await self._release(text, marked, gate, turn.turn_index)
                        released.append(seg)
                        yield seg

        delivered = " ".join(s.text for s in released)
        # the whole turn once more, for the safeguard's audit only: nothing is retracted
        await self._safeguard.pre_response(
            delivered, self._context(turn.turn_index, partial=False)
        )
        for event, data in gate.audit:
            await self._audit(event, data)
        yield TurnResult(
            text=delivered,
            marks=tuple(gate.marks),
            behaviour_class=gate.behaviour_class,
            contest_entries=tuple(gate.contest),
            language=language,
            usage=self._usage(prompt_tokens, self._count(completion)),
            call_records=tuple(records),
            replaced_count=gate.replaced,
            content_hash=hashlib.sha256(delivered.encode("utf-8")).hexdigest(),
            interrupted=interrupted,
        )

    async def _part_b_once(
        self, bundle: ContextBundle, language: str, records: list[ProviderCallRecord]
    ) -> tuple[str, tuple[str, ...], str]:
        """One generation: (checked text, violation codes, raw completion)."""
        messages = self._renderer.part_b_messages(bundle, language)
        it, first, started = await self._open_stream(messages, records)
        first_at = _now()
        raw = first.content if first is not None else ""
        try:
            async for chunk in it:
                raw += chunk.content
                if part_b.word_count(raw) > part_b.MAX_WORDS:
                    break  # the stream stops at the cap; the check below rejects it
        except Exception as exc:
            rec = ProviderCallRecord(
                started, first_at, _now(), "error", type(exc).__name__
            )
            records.append(rec)
            raise ProviderOutageError(rec) from None
        finally:
            closer = getattr(it, "aclose", None)
            if closer is not None:
                await closer()
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
        for attempt in (1, 2):
            if (
                attempt == 2
                and remaining_s is not None
                and loop.time() - t0 >= remaining_s
            ):
                break  # no time left for a second generation
            text, codes, raw = await self._part_b_once(bundle, language, records)
            # every generation is paid for, the rejected one too (REQ-083 accounting)
            prompt_spent += sum(
                self._count(m.content) + MESSAGE_OVERHEAD
                for m in self._renderer.part_b_messages(bundle, language)
            )
            completion_spent += self._count(raw)
            if not codes:
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
                )
            await self._audit(
                "assess_part_b_rejected",
                {"attempt": attempt, "reason_code": ",".join(codes)},
            )
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


async def _next_chunk(
    it: AsyncIterator[CompletionChunk], stop: asyncio.Event | None
) -> CompletionChunk | object | None:
    """The next chunk, ``None`` at the end, or ``_STOPPED`` if ``stop`` is set first."""
    if stop is None:
        return await anext(it, None)
    nxt = asyncio.ensure_future(anext(it, None))
    stopped = asyncio.ensure_future(stop.wait())
    done, _ = await asyncio.wait({nxt, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if nxt in done:
        stopped.cancel()
        return nxt.result()
    nxt.cancel()
    return _STOPPED


async def _close(it: AsyncIterator[CompletionChunk]) -> None:
    closer = getattr(it, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception:
            pass
