"""Verbatim teacher feedback (REQ-096c, post-check stage 5).

Text the model presents as "Commento del docente" must be an exact substring (whitespace
normalised) of the feedback of the item cited in the same sentence: the quiz item's visible review
text, the criterion remark or the overall feedback. With no item cited, any feedback in the bundle
may match. Otherwise the quote is replaced with ``MISSING`` and the turn is marked: a paraphrase
or an invented comment must never be shown to the student as the teacher's words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle

MISSING = "[commento del docente non verificato: vedi la pagina di revisione]"

_QUOTE = re.compile(
    r"(?P<lead>(?:Commento del docente|Teacher(?:'s)? (?:comment|feedback))\s*:?\s*)"
    r"(?P<q>\"[^\"]*\"|“[^”]*”|«[^»]*»)",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r"\b(?:Domanda\s+(\w+)|Criterio\s+(\d+)|(Valutazione complessiva))\b", re.IGNORECASE
)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def feedback_index(bundle: ContextBundle) -> dict[str, str]:
    """Canonical label (lower case) -> the feedback text the student can see for it."""
    out: dict[str, str] = {}
    for i in bundle.quiz_items:
        if i.review_text:
            out[S.QUIZ_LABEL.format(label=i.label).lower()] = i.review_text
    for n, c in enumerate(bundle.criteria, start=1):
        if c.remark:
            out[S.CRITERION_LABEL.format(n=n).lower()] = c.remark
    if bundle.overall.overall_feedback:
        out[S.OVERALL_LABEL.lower()] = bundle.overall.overall_feedback
    return out


@dataclass(frozen=True)
class QuoteCheck:
    text: str
    replaced: int


def check(sentence: str, index: dict[str, str]) -> QuoteCheck:
    cited = []
    for m in _LABEL.finditer(sentence):
        key = (
            f"domanda {m.group(1).lower()}"
            if m.group(1)
            else f"criterio {m.group(2)}"
            if m.group(2)
            else S.OVERALL_LABEL.lower()
        )
        if key in index:
            cited.append(index[key])
    sources = [_norm(t) for t in (cited or index.values())]
    replaced = 0

    def take(m: re.Match[str]) -> str:
        nonlocal replaced
        inner = _norm(m.group("q")[1:-1])
        if inner and any(inner in s for s in sources):
            return m.group(0)
        replaced += 1
        return m.group("lead") + MISSING

    return QuoteCheck(_QUOTE.sub(take, sentence), replaced)
