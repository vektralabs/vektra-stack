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

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

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
CONTEST_UNRELEASED = "marker_contest_unreleased"


class Unit(NamedTuple):
    """One releasable unit. ``system`` units are Vektra-shipped text (a refusal core sentence, the
    BR-009 notice): never model output, so the response safeguard does not rewrite them."""

    text: str
    marked: bool
    system: bool = False
    #: contest points proposed in this unit's sentence: recorded only once the unit is released
    contest: tuple[ContestEntry, ...] = ()


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
    # content-free counts and timing for the trace
    by_stage: Counter[str] = field(default_factory=Counter)
    elapsed_ms: float = 0.0
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
        self.by_stage[code] += max(n, 1)

    def feed(self, sentence: str) -> list[Unit]:
        """The units releasable for this sentence, in order."""
        t = time.perf_counter()
        try:
            return self._feed(sentence)
        finally:
            self.elapsed_ms += (time.perf_counter() - t) * 1000

    def _feed(self, sentence: str) -> list[Unit]:
        out: list[Unit] = []
        m = markers.extract(sentence, self._labels)
        for code in m.dropped:
            self.audit.append(("assess_marker_dropped", {"reason_code": code}))
            self._mark(MARK_MARKER, 0)
        if m.behaviour_class is not None:
            if self._first and self.behaviour_class is None:
                self.behaviour_class = m.behaviour_class
                self._contextual_left = 1
                out.append(
                    Unit(
                        self.shipped.refusal(m.behaviour_class, self.support_text),
                        False,
                        system=True,
                    )
                )
            else:  # a class only opens a reply; a late one is stripped and audited
                self.audit.append(
                    ("assess_marker_dropped", {"reason_code": CLASS_LATE})
                )
                self._mark(MARK_MARKER, 0)
        self._first = False
        text = m.text
        contest = list(m.contest)
        if self._contextual_left == 0:
            # past the one contextual sentence of a behaviour-class reply: never released
            self._drop_contest(contest)
            return out
        if not text:
            # a marker on its own line is the prompt's way to record a point (system.j2): there is
            # no sentence to release, the marker itself is never shown
            self.contest.extend(contest)
            return out
        if self._contextual_left is not None:
            self._contextual_left -= 1
        category = lexicon.scan(text)
        if category is not None:
            self.audit.append(("grade_claim_replaced", {"reason_code": category}))
            self._mark(MARK_LEXICON)
            self._drop_contest(contest)
            out.append(Unit(self.br009, True, system=True))
            return out
        v = values.check(text, self._values, self.shipped.missing["value_unverified"])
        if v.replaced:
            self._mark(MARK_VALUES, v.replaced)
        c = citations.check(v.text, self._labels)
        if c.removed:
            self._mark(MARK_CITATIONS, c.removed)
        q = feedback_quote.check(
            c.text, self._feedback, self.shipped.missing["quote_unverified"]
        )
        if q.replaced:
            self._mark(MARK_QUOTE, q.replaced)
        if q.text.strip():
            marked = bool(v.replaced or c.removed or q.replaced)
            out.append(Unit(q.text, marked, contest=tuple(contest)))
        else:
            self._drop_contest(contest)
        return out

    def _drop_contest(self, entries: Sequence[ContestEntry]) -> None:
        """A contest point whose sentence the student never sees is not recorded."""
        if entries:
            self.audit.append(
                ("assess_marker_dropped", {"reason_code": CONTEST_UNRELEASED})
            )
            self._mark(MARK_MARKER, 0)

    def commit(self, unit: Unit) -> None:
        """Record the unit's contest points: called by the runner once the unit is released."""
        self.contest.extend(unit.contest)
