"""Scoped value check (REQ-096a, post-check stage 3).

Only numbers ASSERTED AS a grade, mark, point or level value are checked: a number bound by pattern
to "punti", "punteggio", "voto", "livello", "/max" (and the English equivalents). Formulas, matrices,
page numbers and the numbers of a calculation are left alone; the false-replacement fixture set
measures that.

A checked value must equal a snapshot value of an item cited in the same sentence (a citation tag
or the bare label). A mismatch, or a value with no item to check it against, is replaced with
``REPLACEMENT`` and the turn is marked. A hidden mark is not in the index, so a stated hidden mark
is always replaced: the check can neither confirm nor reveal it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.snapshot import GradeKind

REPLACEMENT = "[valore non verificato: vedi la pagina di revisione]"

_NUM = r"\d+(?:[.,]\d+)?"
# "3,5/10", "3.5 / 10": the pair is checked together (value and maximum)
_RATIO = re.compile(rf"(?<![\w.,/])({_NUM})\s*/\s*({_NUM})(?![\w/])")
_KEY = r"punt[io]|punteggio|voto|valutazione|livello|mark[s]?|score[ds]?|points?|grade|level"
# "4 punti", "punteggio di 4", "livello 3", "voto: 27", "scored 4", "level 2"
_AFTER = re.compile(
    rf"\b(?:{_KEY})\b(?:\s+(?:è|e'|era|sono|di|pari a|uguale a|of|is|was|are|=|:))?\s*:?\s*({_NUM})(?![\w/.,]\d)",
    re.IGNORECASE,
)
_BEFORE = re.compile(rf"(?<![\w.,/])({_NUM})\s+(?:{_KEY})\b", re.IGNORECASE)
# a ratio is a grade only in a sentence that talks about grades
_GRADE_CONTEXT = re.compile(
    rf"\b(?:{_KEY}|ottenut[oaie]|preso|assegnat[oaie]|got|received|awarded)\b",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r"\b(?:Domanda\s+(\w+)|Criterio\s+(\d+)|(Valutazione complessiva))\b", re.IGNORECASE
)


@dataclass(frozen=True)
class ItemValues:
    pairs: frozenset[
        tuple[Decimal, Decimal]
    ]  # (value, maximum) as the snapshot holds them
    singles: frozenset[Decimal]  # any single value of the item


@dataclass(frozen=True)
class ValueCheck:
    text: str
    replaced: int


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _values(*pairs: tuple[str | int | None, str | int | None]) -> ItemValues:
    ps: set[tuple[Decimal, Decimal]] = set()
    ss: set[Decimal] = set()
    for v, m in pairs:
        dv, dm = (
            _dec(None if v is None else str(v)),
            _dec(None if m is None else str(m)),
        )
        if dv is not None:
            ss.add(dv)
        if dm is not None:
            ss.add(dm)
        if dv is not None and dm is not None:
            ps.add((dv, dm))
    return ItemValues(frozenset(ps), frozenset(ss))


def value_index(bundle: ContextBundle) -> dict[str, ItemValues]:
    """Canonical label (lower case) -> the values the student may see for it."""
    out: dict[str, ItemValues] = {}
    for i in bundle.quiz_items:
        out[S.QUIZ_LABEL.format(label=i.label).lower()] = _values((i.mark, i.max_mark))
    for n, c in enumerate(bundle.criteria, start=1):
        out[S.CRITERION_LABEL.format(n=n).lower()] = _values(
            (c.points, c.max_points), (c.level_index, c.level_count)
        )
    ov = bundle.overall
    if ov.grade_kind is GradeKind.POINTS:
        out[S.OVERALL_LABEL.lower()] = _values(
            (ov.grade, ov.grade_max), (ov.grade_display, ov.grade_max)
        )
    else:
        out[S.OVERALL_LABEL.lower()] = _values()  # a scale has a label, never a number
    return out


def _cited(sentence: str) -> list[str]:
    out = []
    for m in _LABEL.finditer(sentence):
        if m.group(1):
            out.append(f"domanda {m.group(1).lower()}")
        elif m.group(2):
            out.append(f"criterio {m.group(2)}")
        else:
            out.append(S.OVERALL_LABEL.lower())
    return out


def check(
    sentence: str, index: dict[str, ItemValues], replacement: str = REPLACEMENT
) -> ValueCheck:
    """``replacement`` is the reply language's shipped phrase (Italian by default)."""
    cited = [index[k] for k in _cited(sentence) if k in index]
    spans: list[tuple[int, int]] = []

    def bad_pair(a: str, b: str) -> bool:
        da, db = _dec(a), _dec(b)
        return not any((da, db) in it.pairs for it in cited)

    def bad_single(a: str) -> bool:
        da = _dec(a)
        return not any(da in it.singles for it in cited)

    ratio_spans: list[tuple[int, int]] = []
    if _GRADE_CONTEXT.search(sentence):
        for m in _RATIO.finditer(sentence):
            ratio_spans.append(m.span())
            if bad_pair(m.group(1), m.group(2)):
                spans.append(m.span())
    for rx in (_AFTER, _BEFORE):
        for m in rx.finditer(sentence):
            s, e = m.span(1)
            if any(rs <= s < re_ for rs, re_ in ratio_spans):
                continue  # already checked as part of its pair
            if bad_single(m.group(1)):
                spans.append((s, e))
    if not spans:
        return ValueCheck(sentence, 0)
    merged: list[tuple[int, int]] = []
    for s, e in sorted(set(spans)):
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    out, last = [], 0
    for s, e in merged:
        out.append(sentence[last:s])
        out.append(replacement)
        last = e
    out.append(sentence[last:])
    return ValueCheck("".join(out), len(merged))


def asserts_any_value(text: str) -> bool:
    """Part B (REQ-094) allows no grade value at all: any grade-bound number or any ratio."""
    return bool(_RATIO.search(text) or _AFTER.search(text) or _BEFORE.search(text))
