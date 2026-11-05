"""Byte-level release discipline (plan assess-llm-runner, task D; REQ-096, REQ-097, NFR-022 CI tier).

The hostile provider emits every forbidden pattern; the released bytes, the concatenation of every
``ReleasedSegment`` the caller receives, must hold none of them. Ordering is instrumented: a
segment is yielded only after its sentence went through the whole gate and its per-sentence
``pre_response``, never from a partial buffer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fakes import (
    FORBIDDEN,
    HOSTILE,
    RecordingAuditSink,
    RecordingSafeguard,
    RecordingTraceWriter,
    ScriptedHostileLLM,
    TimedLLM,
)
from test_context_bundle import _ALL, _with_review
from test_context_snapshot import make

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm import DiscussionTurnRunner, ReleasedSegment, StudentTurn
from vektra_assess.llm.postcheck import gate as gate_mod


def _bundle():  # type: ignore[no-untyped-def]
    s = make()
    return assemble_context(s, _with_review(s, _ALL, "unreadable"))


def _runner(llm: Any, guard: RecordingSafeguard) -> DiscussionTurnRunner:
    return DiscussionTurnRunner(
        llm=llm,
        safeguard=guard,
        trace_writer=RecordingTraceWriter(),
        settings=VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_LLM_MODEL": "t/m"}
        ),
        audit=RecordingAuditSink(),
    )


async def _released(llm: Any, events: list[tuple[str, str]] | None = None) -> bytes:
    guard = RecordingSafeguard()
    out: list[str] = []
    async for item in _runner(llm, guard).run_stream(
        _bundle(), StudentTurn("perché?", 1)
    ):
        if isinstance(item, ReleasedSegment):
            if events is not None:
                events.append(("yield", item.text))
            out.append(item.text)
    return " ".join(out).encode("utf-8")


@pytest.mark.parametrize("name", sorted(HOSTILE))
async def test_each_hostile_reply_releases_no_forbidden_byte(name: str) -> None:
    data = await _released(ScriptedHostileLLM(name))
    for pattern in FORBIDDEN:
        assert pattern.encode() not in data, (name, pattern, data)


async def test_all_hostile_replies_in_one_turn_release_no_forbidden_byte() -> None:
    data = await _released(ScriptedHostileLLM())
    hits = [p for p in FORBIDDEN if p.encode() in data]
    assert hits == [], hits
    assert data  # something was released: the test is not passing on an empty reply


async def test_character_by_character_streaming_releases_no_forbidden_byte() -> None:
    """The finest possible chunking: a check that only worked on whole words would fail here."""
    text = " ".join(HOSTILE.values())
    data = await _released(TimedLLM([(0, ch) for ch in text]))
    assert [p for p in FORBIDDEN if p.encode() in data] == []


async def test_no_segment_is_yielded_before_its_sentence_passed_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []
    original = gate_mod.SentenceGate.feed

    def spy(self: gate_mod.SentenceGate, sentence: str) -> list[tuple[str, bool]]:
        out = original(self, sentence)
        events.append(("gate", sentence))
        for text, _ in out:
            events.append(("passed", text))
        return out

    monkeypatch.setattr(gate_mod.SentenceGate, "feed", spy)
    text = "Prima frase completa. " + " ".join(HOSTILE.values()) + " Ultima frase."
    await _released(TimedLLM([(0, ch) for ch in text]), events)
    yields = [i for i, e in enumerate(events) if e[0] == "yield"]
    assert yields
    for i in yields:
        released = events[i][1]
        passed = [j for j, e in enumerate(events[:i]) if e == ("passed", released)]
        if released != "[risposta sostituita dal sistema]":
            assert passed, f"segment yielded without passing the gate: {released!r}"
        # every gate input was a whole sentence: it ends with a terminator, a closing quote or
        # bracket, or is the final flush
    gate_inputs = [e[1] for e in events if e[0] == "gate"]
    assert all(s.rstrip()[-1] in '.?!"”»]' for s in gate_inputs[:-1]), gate_inputs


async def test_canary_the_check_bites_when_a_stage_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the lexicon must let its patterns through: the zero above is not vacuous."""
    from vektra_assess.llm.postcheck import lexicon

    monkeypatch.setattr(lexicon, "scan", lambda _s: None)
    data = await _released(ScriptedHostileLLM("verdict"))
    assert b"Hai ragione" in data
