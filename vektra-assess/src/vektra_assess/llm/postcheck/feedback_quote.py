"""Verbatim teacher feedback (REQ-096c, post-check stage 5).

Text the model presents as "Commento del docente" must be an exact substring (whitespace
normalised) of the feedback of the item cited in the same sentence: the quiz item's visible review
text, the criterion remark or the overall feedback. A sentence that cites an item is checked
against that item's feedback only: a cited item that is not in the bundle, or has no feedback, can
confirm no quote. Only a sentence with no item cited may match any feedback in the bundle.
Otherwise the quote is replaced with ``MISSING`` and the turn is marked: a paraphrase or an invented
comment must never be shown to the student as the teacher's words. A quotation opened after a lead
and never closed in the sentence (cut by the splitter's hold limit or by the end of the stream) is
replaced from its opener to the end of the sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.llm.postcheck.values import cited_labels

MISSING = "[commento del docente non verificato: vedi la pagina di revisione]"

#: the words that present a quotation as the teacher's (shared with the sentence splitter)
LEAD = r"(?:Commento del docente(?:\s+dice)?|Teacher(?:['\u2019]s)? (?:comment|feedback)(?:\s+says)?)"
_QUOTE = re.compile(
    rf"(?P<lead>{LEAD}\s*:?\s*)"
    r"(?P<q>\"[^\"]*\"|“[^”]*”|«[^»]*»)",
    re.IGNORECASE,
)
# a quotation opened after a lead with no closer before the end of the sentence
_OPEN_QUOTE = re.compile(
    rf"(?P<lead>{LEAD}\s*:?\s*)(?:\"[^\"]*|“[^”]*|«[^»]*)$",
    re.IGNORECASE | re.DOTALL,
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


def check(sentence: str, index: dict[str, str], missing: str = MISSING) -> QuoteCheck:
    keys = [k for _, k in cited_labels(sentence, index)]
    # a citation restricts the sources to the cited items, even when none of them has feedback
    pool = [index[k] for k in keys if k in index] if keys else list(index.values())
    sources = [_norm(t) for t in pool]
    replaced = 0

    def take(m: re.Match[str]) -> str:
        nonlocal replaced
        inner = _norm(m.group("q")[1:-1])
        if inner and any(inner in s for s in sources):
            return m.group(0)
        replaced += 1
        return m.group("lead") + missing

    def cut(m: re.Match[str]) -> str:
        nonlocal replaced
        replaced += 1
        return m.group("lead") + missing

    text = _QUOTE.sub(take, sentence)
    text = _OPEN_QUOTE.sub(cut, text)
    return QuoteCheck(text, replaced)
