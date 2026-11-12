"""DiscussionTurnRunner stream edges (PR #150 review): one open deadline, empty chunks, stop
before and during release, stall after the first token, history anonymisation, Part B budget."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakes import RecordingAuditSink as _Audit
from fakes import RecordingSafeguard as Guard
from fakes import RecordingTraceWriter as _Trace
from fakes import TimedLLM
from test_llm_runner import (
    PART_B_OK,
    _Metrics,
    _timed_runner,
    _visible_bundle,
    settings,
)

from vektra_assess.llm import (
    DiscussionTurnRunner,
    ProviderOutageError,
    ReleasedSegment,
    StudentTurn,
    TurnResult,
)
from vektra_assess.llm.budget import ContextOverflowError
from vektra_assess.llm.types import PriorTurn
from vektra_shared.types import CompletionChunk, QueryTrace, SafeguardResult
from vektra_testkit.recorders import RecordingLLMProvider


class _SlowOpen(TimedLLM):
    """The provider takes ``open_s`` to return its stream at all."""

    def __init__(self, open_s: float, chunks: list[tuple[float, str]]) -> None:
        super().__init__(chunks)
        self.open_s = open_s

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[CompletionChunk]:  # type: ignore[override]
        await asyncio.sleep(self.open_s)
        return await super().stream(*a, **kw)


async def _items(
    r: DiscussionTurnRunner, turn: StudentTurn, stop: asyncio.Event | None
):  # type: ignore[no-untyped-def]
    return [i async for i in r.run_stream(_visible_bundle(), turn, stop)]


async def test_one_deadline_covers_the_open_and_the_first_token() -> None:
    # open 0.15 s + first token 0.15 s: each alone is under 0.25 s, together they are not
    llm = _SlowOpen(0.15, [(0.15, "Tardi.")])
    r, _ = _timed_runner(llm, timeout=0.25)
    with pytest.raises(ProviderOutageError) as e:
        await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert e.value.record.outcome == "first_token_timeout" and llm.closed


async def test_empty_chunks_do_not_count_as_the_first_token() -> None:
    llm = TimedLLM([(0, ""), (0, ""), (0, "Ecco."), (0, " Fine.")])
    r, _ = _timed_runner(llm)
    out = await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert out.text == "Ecco. Fine."


async def test_a_stream_without_any_text_is_an_outage_not_an_empty_reply() -> None:
    llm = TimedLLM([(0, ""), (0, "")])
    r, _ = _timed_runner(llm)
    with pytest.raises(ProviderOutageError) as e:
        await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert e.value.record.error_class == "EmptyStream" and llm.closed


async def test_a_first_chunk_with_a_whole_sentence_is_released_at_once() -> None:
    llm = TimedLLM([(0, "Prima frase. "), (5.0, "mai")])
    r, _ = _timed_runner(llm, timeout=10)
    gen = r.run_stream(_visible_bundle(), StudentTurn("ciao", 0))
    first = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(first, ReleasedSegment) and first.text == "Prima frase."
    await gen.aclose()


async def test_stop_already_set_means_no_model_call() -> None:
    llm = TimedLLM([(0, "Mai.")])
    r, _ = _timed_runner(llm)
    stop = asyncio.Event()
    stop.set()
    items = await _items(r, StudentTurn("ciao", 0), stop)
    (result,) = items
    assert isinstance(result, TurnResult) and result.interrupted and result.text == ""
    assert result.call_records[-1].outcome == "cancelled"
    assert not [c for c in llm.calls if c.method == "stream"]


async def test_stop_during_the_first_token_wait_is_a_cancel_not_an_outage() -> None:
    llm = _SlowOpen(0.0, [(5.0, "Mai.")])
    r, _ = _timed_runner(llm, timeout=10)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    items = await asyncio.wait_for(_items(r, StudentTurn("ciao", 0), stop), 2.0)
    result = items[-1]
    assert isinstance(result, TurnResult) and result.interrupted
    assert result.call_records[-1].outcome == "cancelled" and llm.closed


async def test_stop_between_buffered_sentences_releases_no_more() -> None:
    llm = TimedLLM([(0, "Uno. Due. Tre. "), (5.0, "mai")])
    r, _ = _timed_runner(llm, timeout=10)
    stop = asyncio.Event()
    items = []
    async for item in r.run_stream(_visible_bundle(), StudentTurn("ciao", 0), stop):
        items.append(item)
        if isinstance(item, ReleasedSegment):
            stop.set()
    result = items[-1]
    assert isinstance(result, TurnResult) and result.interrupted
    assert result.text == "Uno."


async def test_a_turn_stream_that_stalls_after_released_text_is_an_outage_with_its_trace() -> (
    None
):
    llm = TimedLLM(
        [(0, "Il docente potrebbe alzare il voto di un punto. "), (5.0, "mai")]
    )
    audit = _Audit()
    writer = _Trace()
    r = DiscussionTurnRunner(
        llm=llm, safeguard=Guard(), trace_writer=writer,
        settings=settings(VEKTRA_ASSESS_OUTAGE_TIMEOUT_S=0.1), audit=audit,
    )  # fmt: skip
    with pytest.raises(ProviderOutageError) as e:
        await _items(r, StudentTurn("ciao", 0), None)
    assert e.value.record.error_class == "StreamStall" and llm.closed
    assert len(writer.rows) == 1  # the failed turn is traced
    assert any(e == "grade_claim_replaced" for e, _ in audit.rows)  # audits flushed


async def test_an_empty_query_rewrite_is_sent_empty() -> None:
    class Blank(Guard):
        async def pre_query(self, query: str, context: Any) -> SafeguardResult:
            await super().pre_query(query, context)
            return SafeguardResult(allowed=True, modified_content="")

    llm = RecordingLLMProvider(["Ok."])
    r = DiscussionTurnRunner(
        llm=llm, safeguard=Blank(), trace_writer=_Trace(), settings=settings(),
        audit=_Audit(),
    )  # fmt: skip
    await r.run(_visible_bundle(), StudentTurn("ORIGINALE-SEGRETO", 1))
    (stream,) = [c for c in llm.calls if c.method == "stream"]
    assert "ORIGINALE-SEGRETO" not in stream.request_bytes.decode()


async def test_earlier_student_messages_are_anonymised_before_the_prompt() -> None:
    class Anon(Guard):
        async def pre_query(self, query: str, context: Any) -> SafeguardResult:
            await super().pre_query(query, context)
            if "Mario Rossi" in query:
                return SafeguardResult(
                    allowed=True,
                    modified_content=query.replace("Mario Rossi", "<PERSON>"),
                )
            if "BLOCCA" in query:
                return SafeguardResult(allowed=False)
            return SafeguardResult()

    llm = RecordingLLMProvider(["Ok."])
    r = DiscussionTurnRunner(
        llm=llm, safeguard=Anon(), trace_writer=_Trace(), settings=settings(),
        audit=_Audit(),
    )  # fmt: skip
    history = (
        PriorTurn("student", "Sono Mario Rossi."),
        PriorTurn("assistant", "Ciao, Mario Rossi."),  # delivered text: left as is
        PriorTurn("student", "BLOCCA questo."),
    )
    await r.run(_visible_bundle(), StudentTurn("E adesso?", 2, history=history))
    body = next(c for c in llm.calls if c.method == "stream").request_bytes.decode()
    assert "Sono Mario Rossi" not in body and "Sono <PERSON>" in body
    assert "BLOCCA" not in body and "Ciao, Mario Rossi." in body


async def test_an_oversized_part_b_prompt_fails_before_generation() -> None:
    llm = RecordingLLMProvider([PART_B_OK])
    r = DiscussionTurnRunner(
        llm=llm, safeguard=Guard(), trace_writer=_Trace(),
        settings=settings(VEKTRA_ASSESS_LLM_CONTEXT_WINDOW=2048, VEKTRA_ASSESS_LLM_MAX_TOKENS=2000),
        audit=_Audit(),
    )  # fmt: skip
    with pytest.raises(ContextOverflowError):
        await r.run_part_b(_visible_bundle())
    assert not [c for c in llm.calls if c.method == "stream"]


async def test_part_b_counts_every_completed_call() -> None:
    m = _Metrics()
    r = DiscussionTurnRunner(
        llm=RecordingLLMProvider(["Il voto è 7.", PART_B_OK]), safeguard=Guard(),
        trace_writer=_Trace(), settings=settings(), audit=_Audit(), metrics=m,
    )  # fmt: skip
    await r.run_part_b(_visible_bundle())
    assert m.events.count(("outcome", "ok", 1)) == 2


async def test_a_failing_audit_after_a_failing_trace_still_completes_the_turn() -> None:
    class Broken(_Trace):
        async def write(self, trace: QueryTrace, namespace: str) -> None:
            raise RuntimeError("db down")

    class BrokenAudit(_Audit):
        async def __call__(self, event: str, data: Any) -> None:
            if event == "assess_trace_write_failed":
                raise RuntimeError("audit down")
            await super().__call__(event, data)

    r = DiscussionTurnRunner(
        llm=RecordingLLMProvider(["Ok."]), safeguard=Guard(), trace_writer=Broken(),
        settings=settings(), audit=BrokenAudit(),
    )  # fmt: skip
    result = await r.run(_visible_bundle(), StudentTurn("ciao", 0))
    assert result.text == "Ok."


# --- contest points follow the sentence the student actually sees (gate.py review) --------------

_CONTEST = (
    '[[PUNTO_CONTESTATO label="Domanda 2" status="proposed"]] Lo segno per il docente.'
)


async def _contest_run(
    reply: str, guard: Guard | None = None
) -> tuple[TurnResult, _Audit]:
    audit = _Audit()
    r = DiscussionTurnRunner(
        llm=RecordingLLMProvider([reply]), safeguard=guard or Guard(),
        trace_writer=_Trace(), settings=settings(), audit=audit,
    )  # fmt: skip
    return await r.run(_visible_bundle(), StudentTurn("ciao", 1)), audit


async def test_a_released_sentence_records_its_contest_point() -> None:
    result, _ = await _contest_run(_CONTEST)
    assert [e.label for e in result.contest_entries] == ["Domanda 2"]


async def test_a_contest_point_past_the_contextual_sentence_is_not_recorded() -> None:
    result, audit = await _contest_run("[[CLASS R-SOL]] Capisco. " + _CONTEST)
    assert result.contest_entries == ()
    assert ("assess_marker_dropped", {"reason_code": "marker_contest_unreleased"}) in (
        audit.rows
    )


async def test_a_contest_point_in_a_refused_sentence_is_not_recorded() -> None:
    class Refuse(Guard):
        async def pre_response(self, text: str, context: Any) -> SafeguardResult:
            await super().pre_response(text, context)
            if "segno" in text and context.metadata.get("partial"):
                return SafeguardResult(allowed=False)
            return SafeguardResult()

    result, _ = await _contest_run(_CONTEST, Refuse())
    assert result.contest_entries == ()


async def test_a_marker_line_on_its_own_is_recorded() -> None:
    result, _ = await _contest_run(
        'Capisco la tua obiezione. [[PUNTO_CONTESTATO label="Domanda 2" status="proposed"]]'
    )
    assert [e.label for e in result.contest_entries] == ["Domanda 2"]


async def test_a_marker_line_past_the_contextual_sentence_is_not_recorded() -> None:
    result, _ = await _contest_run(
        '[[CLASS R-SOL]] Capisco. Altro. [[PUNTO_CONTESTATO label="Domanda 2" status="proposed"]]'
    )
    assert result.contest_entries == ()


async def test_stop_keeps_later_sentences_out_of_the_gate() -> None:
    llm = TimedLLM([(0, "Uno. [[CLASS R-SOL]] Due. Tre. "), (5.0, "mai")])
    r, audit = _timed_runner(llm, timeout=10)
    stop = asyncio.Event()
    items = []
    async for item in r.run_stream(_visible_bundle(), StudentTurn("ciao", 0), stop):
        items.append(item)
        if isinstance(item, ReleasedSegment):
            stop.set()
    result = items[-1]
    assert isinstance(result, TurnResult) and result.text == "Uno."
    assert not audit.rows  # the late class marker was never fed, so never audited


def test_spelled_out_values_are_replaced_in_a_turn() -> None:
    from vektra_assess.llm.postcheck import values

    for sentence in (
        "Nella [Domanda 2] hai preso quattro punti.",
        "Your grade is seven.",
    ):
        out = values.check(sentence, {})
        assert out.replaced == 1 and values.REPLACEMENT in out.text, sentence
    assert values.check("Hai un punto di vista valido.", {}).replaced == 0
