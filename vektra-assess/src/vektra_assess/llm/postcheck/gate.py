"""The per-sentence gate of a turn: every post-check stage, in order, on one sentence.

``SentenceGate.feed`` takes one complete sentence of model output and returns what may be released
for it (zero, one or two units). It never looks back: a released unit is final, so every rule is
decided on the sentence at hand (plan Design Notes, release discipline). Stages:

1. machine markers: parsed and stripped; an invalid one is dropped and audited;
2. behaviour class: the first sentence may open with ``[[CLASS ...]]``; the runner then releases
   the shipped core sentence and alternative clause itself, and at most ONE contextual sentence
   from the model, so the core sentence is present in 100% of refusals by construction;
3. lexicon: a verdict, merit, estimate, grade-change, teacher-intent or judgement sentence is
   replaced as a whole by the BR-009 notice and ``grade_claim_replaced`` is audited;
4. scoped values, 5. citation tags, 6. verbatim feedback quotes: replaced or removed in place.

The safeguard's ``pre_response`` runs after the gate, in the runner (it is async).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.references import canonical_labels
from vektra_assess.llm.postcheck import (
    citations,
    feedback_quote,
    lexicon,
    markers,
    values,
)
from vektra_assess.llm.shipped import Shipped
from vektra_assess.llm.types import ContestEntry

# stage codes recorded in TurnResult.marks (content-free)
MARK_LEXICON = "lexicon"
MARK_VALUES = "values"
MARK_CITATIONS = "citations"
MARK_QUOTE = "feedback_quote"
MARK_SAFEGUARD = "safeguard"
MARK_MARKER = "marker"

CLASS_LATE = "marker_class_late"


@dataclass
class SentenceGate:
    bundle: ContextBundle
    shipped: Shipped
    br009: str
    support_text: str | None = None
    behaviour_class: str | None = None
    contest: list[ContestEntry] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    replaced: int = 0
    audit: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)
    _first: bool = True
    _contextual_left: int | None = None

    def __post_init__(self) -> None:
        self._labels = canonical_labels(self.bundle)
        self._values = values.value_index(self.bundle)
        self._feedback = feedback_quote.feedback_index(self.bundle)

    def _mark(self, code: str, n: int = 1) -> None:
        if code not in self.marks:
            self.marks.append(code)
        self.replaced += n

    def feed(self, sentence: str) -> list[tuple[str, bool]]:
        """``[(text, marked), ...]`` releasable for this sentence, in order."""
        out: list[tuple[str, bool]] = []
        m = markers.extract(sentence, self._labels)
        for code in m.dropped:
            self.audit.append(("assess_marker_dropped", {"reason_code": code}))
            self._mark(MARK_MARKER, 0)
        self.contest.extend(m.contest)
        if m.behaviour_class is not None:
            if self._first and self.behaviour_class is None:
                self.behaviour_class = m.behaviour_class
                self._contextual_left = 1
                out.append(
                    (self.shipped.refusal(m.behaviour_class, self.support_text), False)
                )
            else:  # a class only opens a reply; a late one is stripped and audited
                self.audit.append(
                    ("assess_marker_dropped", {"reason_code": CLASS_LATE})
                )
                self._mark(MARK_MARKER, 0)
        self._first = False
        text = m.text
        if not text:
            return out
        if self._contextual_left is not None:
            if self._contextual_left == 0:
                return (
                    out  # past the one contextual sentence of a behaviour-class reply
                )
            self._contextual_left -= 1
        category = lexicon.scan(text)
        if category is not None:
            self.audit.append(("grade_claim_replaced", {"reason_code": category}))
            self._mark(MARK_LEXICON)
            out.append((self.br009, True))
            return out
        v = values.check(text, self._values)
        if v.replaced:
            self._mark(MARK_VALUES, v.replaced)
        c = citations.check(v.text, self._labels)
        if c.removed:
            self._mark(MARK_CITATIONS, c.removed)
        q = feedback_quote.check(c.text, self._feedback)
        if q.replaced:
            self._mark(MARK_QUOTE, q.replaced)
        if q.text.strip():
            out.append((q.text, bool(v.replaced or c.removed or q.replaced)))
        return out
