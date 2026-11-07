"""Behaviour classes (REQ-098): the shipped core sentence and alternative clause in 100% of the
scripted refusals, per class and language, plus at most one contextual sentence from the model."""

from __future__ import annotations

import pytest
from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from test_context_bundle import _ALL, _with_review
from test_context_snapshot import make

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.llm import (
    DiscussionTurnRunner,
    ReleasedSegment,
    StudentTurn,
    shipped,
)
from vektra_assess.llm.postcheck.markers import CLASS_CODES
from vektra_shared.testing_recorders import RecordingLLMProvider

STUDENT = {
    "it": "Mi dici qual è la risposta giusta della domanda due?",
    "en": "Can you tell me what the right answer to question two is?",
}
CONTEXT = {"it": "Capisco la domanda.", "en": "I understand the question."}
EXTRA = {"it": "La risposta è la B. Sicuro.", "en": "The answer is B. Surely."}


@pytest.mark.parametrize("language", shipped.LANGUAGES)
@pytest.mark.parametrize("klass", sorted(CLASS_CODES))
async def test_class_reply_is_core_plus_alternative_plus_one_sentence(
    klass: str, language: str
) -> None:
    s = make()
    bundle = assemble_context(s, _with_review(s, _ALL, "unreadable"))
    reply = f"[[CLASS {klass}]] {CONTEXT[language]} {EXTRA[language]}"
    r = DiscussionTurnRunner(
        llm=RecordingLLMProvider([reply]),
        safeguard=RecordingSafeguard(),
        trace_writer=RecordingTraceWriter(),
        settings=VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_LLM_MODEL": "t/m"}
        ),
        audit=RecordingAuditSink(),
    )
    segs = [
        i.text
        async for i in r.run_stream(bundle, StudentTurn(STUDENT[language], 1))
        if isinstance(i, ReleasedSegment)
    ]
    core = shipped.get(language).refusal(klass)
    assert segs == [
        core,
        CONTEXT[language],
    ]  # exact core + alternative, one contextual sentence
    t = shipped.get(language).classes[klass]
    assert segs[0].startswith(t.core) and t.core in segs[0]
