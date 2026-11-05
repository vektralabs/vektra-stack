"""Scoped value check (REQ-096a, post-check stage 3).

Only numbers ASSERTED AS a grade, mark, point or level value are checked: a number bound by pattern
to "punti", "punteggio", "voto", "livello", "/max" (and the English equivalents). Formulas, matrices,
page numbers and the numbers of a calculation are left alone; the false-replacement fixture set
measures that.

A checked value must equal a snapshot value of the item it is attributed to: the label cited
closest before it in the sentence (a citation tag or the bare label), or the only label cited after
it. Values keep their role: points obtained match only an earned mark, a value presented as the
maximum ("su 5", "vale 5 punti", "out of 5") only a maximum, a level only a level, and a ratio
("2,5/5", "2,5 su 5", "2.5 out of 5") is checked as a pair. A mismatch, a value no item can be
attributed to with certainty, or an item that is not in the bundle is replaced with ``REPLACEMENT``
and the turn is marked. A hidden mark is not in the index, so a stated hidden mark is always
replaced, even when it equals the visible maximum: the check can neither confirm nor reveal it.
Signs are kept (``-1`` is not ``1``).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.snapshot import GradeKind

REPLACEMENT = "[valore non verificato: vedi la pagina di revisione]"

_NUM = r"[-\u2212]?\d+(?:[.,]\d+)?"  # an optional ASCII or Unicode minus sign is part of the value
_PTS = r"(?:punt[io]|points?)"
# "3,5/10", "3.5 / 10", "2,5 su 5", "2,5 punti su 5", "2.5 out of 5": value and maximum together
_RATIO = re.compile(
    rf"(?<![\w.,/])({_NUM})\s*(?:{_PTS}\s+)?(?:/|\bsu\b|\bout\s+of\b)\s*({_NUM})(?![\w/])",
    re.IGNORECASE,
)
_LEVEL_KEY = r"livell[oi]|levels?"
_KEY = rf"punt[io]|punteggio|voto|valutazione|{_LEVEL_KEY}|mark[s]?|score[ds]?|points?|grade"
_CONNECT = r"è|e'|era|sono|di|pari a|uguale a|of|is|was|are|=|:"
# "4 punti", "punteggio di 4", "livello 3", "voto: 27", "scored 4", "level 2"
_AFTER = re.compile(
    rf"\b(?P<key>{_KEY})\b(?:\s+(?:{_CONNECT}))?\s*:?\s*({_NUM})(?![\w/.,]\d)",
    re.IGNORECASE,
)
_BEFORE = re.compile(rf"(?<![\w.,/])({_NUM})\s+(?P<key>{_KEY})\b", re.IGNORECASE)
_IS_LEVEL = re.compile(rf"^(?:{_LEVEL_KEY})$", re.IGNORECASE)
# the words before a value that present it as the maximum of the item
_MAX_CONTEXT = re.compile(
    r"\b(?:vale(?:va)?|massim[oa]|max(?:imum)?|su(?: un totale di)?|out\s+of|worth|possible|"
    r"totale)(?:\s+(?:di|è|e'|of|is))?\s*:?\s*$",
    re.IGNORECASE,
)
# a ratio is a grade only in a sentence that talks about grades
_GRADE_CONTEXT = re.compile(
    rf"\b(?:{_KEY}|ottenut[oaie]|preso|assegnat[oaie]|got|received|awarded)\b",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r"\b(?:Domanda\s+(\w+)|Criterio\s+(\d+)|(Valutazione complessiva))\b", re.IGNORECASE
)

# spelled-out values, for Part B only ("quattro punti", "Your grade is seven"). Articles that are
# also numbers ("un punto", "a point", "one point") are not counted before a key
_WORDS_IT = (
    "zero due tre quattro cinque sei sette otto nove dieci undici dodici tredici quattordici "
    "quindici sedici diciassette diciotto diciannove venti ventuno ventidue ventitré ventitre "
    "ventiquattro venticinque ventisei ventisette ventotto ventinove trenta trentuno mezzo"
)
_WORDS_EN = (
    "zero two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty half"
)
_SPELLED = "|".join(
    sorted((_WORDS_IT + " " + _WORDS_EN).split(), key=len, reverse=True)
)
_SPELLED_AFTER = re.compile(
    rf"\b(?:{_KEY})\s+(?:{_CONNECT})\s*:?\s*(?:{_SPELLED}|uno|one)\b"
    r"(?!\s+(?:of|dei|degli|delle|di|del|della)\b)",
    re.IGNORECASE,
)
_SPELLED_BEFORE = re.compile(rf"\b(?:{_SPELLED})\s+(?:{_KEY})\b", re.IGNORECASE)


@dataclass(frozen=True)
class ItemValues:
    pairs: frozenset[
        tuple[Decimal, Decimal]
    ]  # (mark, maximum) as the snapshot holds them
    earned: frozenset[Decimal]  # marks or points obtained
    maxima: frozenset[Decimal]  # maximum marks or points
    level_pairs: frozenset[tuple[Decimal, Decimal]] = (
        frozenset()
    )  # (level, level count)
    levels: frozenset[Decimal] = frozenset()  # the level reached


@dataclass(frozen=True)
class ValueCheck:
    text: str
    replaced: int


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.replace(",", ".").replace("\u2212", "-"))
    except InvalidOperation:
        return None


def _role(
    *pairs: tuple[str | int | None, str | int | None],
) -> tuple[set[tuple[Decimal, Decimal]], set[Decimal], set[Decimal]]:
    ps: set[tuple[Decimal, Decimal]] = set()
    vs: set[Decimal] = set()
    ms: set[Decimal] = set()
    for v, m in pairs:
        dv = _dec(None if v is None else str(v))
        dm = _dec(None if m is None else str(m))
        if dv is not None:
            vs.add(dv)
        if dm is not None:
            ms.add(dm)
        if dv is not None and dm is not None:
            ps.add((dv, dm))
    return ps, vs, ms


def _values(
    *pairs: tuple[str | int | None, str | int | None],
    level: tuple[int | None, int | None] | None = None,
) -> ItemValues:
    ps, vs, ms = _role(*pairs)
    lps, ls, _ = _role(level) if level is not None else (set(), set(), set())
    return ItemValues(
        frozenset(ps), frozenset(vs), frozenset(ms), frozenset(lps), frozenset(ls)
    )


def value_index(bundle: ContextBundle) -> dict[str, ItemValues]:
    """Canonical label (lower case) -> the values the student may see for it."""
    out: dict[str, ItemValues] = {}
    for i in bundle.quiz_items:
        out[S.QUIZ_LABEL.format(label=i.label).lower()] = _values((i.mark, i.max_mark))
    for n, c in enumerate(bundle.criteria, start=1):
        out[S.CRITERION_LABEL.format(n=n).lower()] = _values(
            (c.points, c.max_points), level=(c.level_index, c.level_count)
        )
    ov = bundle.overall
    if ov.grade_kind is GradeKind.POINTS:
        out[S.OVERALL_LABEL.lower()] = _values(
            (ov.grade, ov.grade_max), (ov.grade_display, ov.grade_max)
        )
    else:
        out[S.OVERALL_LABEL.lower()] = _values()  # a scale has a label, never a number
    return out


def cited_labels(sentence: str, known: Collection[str] = ()) -> list[tuple[int, str]]:
    """``(position, canonical key)`` of each label the sentence cites, in order.

    "Domanda <x>" is a citation when ``x`` starts with a digit or names a known item: in running
    text "la domanda chiedeva" is not one. A numbered label that is not known is kept (it cites an
    item the check cannot confirm)."""
    out = []
    for m in _LABEL.finditer(sentence):
        if m.group(1):
            key = f"domanda {m.group(1).lower()}"
            if key not in known and not m.group(1)[0].isdigit():
                continue
        elif m.group(2):
            key = f"criterio {m.group(2)}"
        else:
            key = S.OVERALL_LABEL.lower()
        out.append((m.start(), key))
    return out


def _attributed(cites: list[tuple[int, str]], at: int) -> str | None:
    """The label a value at ``at`` belongs to, or None when that cannot be told with certainty."""
    before = [k for p, k in cites if p < at]
    if before:
        return before[-1]
    after = {k for p, k in cites if p > at}
    return after.pop() if len(after) == 1 else None


def check(
    sentence: str, index: dict[str, ItemValues], replacement: str = REPLACEMENT
) -> ValueCheck:
    """``replacement`` is the reply language's shipped phrase (Italian by default)."""
    cites = cited_labels(sentence, index)
    talks_levels = re.search(rf"\b(?:{_LEVEL_KEY})\b", sentence, re.IGNORECASE)
    spans: list[tuple[int, int]] = []

    def item_at(at: int) -> ItemValues | None:
        key = _attributed(cites, at)
        return None if key is None else index.get(key)

    def bad_pair(at: int, a: str, b: str) -> bool:
        it, pair = item_at(at), (_dec(a), _dec(b))
        if it is None:
            return True
        return not (pair in it.pairs or (talks_levels and pair in it.level_pairs))

    def bad_single(at: int, a: str, key: str, before: str) -> bool:
        it, da = item_at(at), _dec(a)
        if it is None or da is None:
            return True
        if _IS_LEVEL.match(key):
            return da not in it.levels
        if _MAX_CONTEXT.search(before):
            return da not in it.maxima
        return da not in it.earned

    ratio_spans: list[tuple[int, int]] = []
    if _GRADE_CONTEXT.search(sentence):
        for m in _RATIO.finditer(sentence):
            ratio_spans.append(m.span())
            if bad_pair(m.start(), m.group(1), m.group(2)):
                spans.append(m.span())
    for m in _AFTER.finditer(sentence):
        s, e = m.span(2)
        if any(rs <= s < re_ for rs, re_ in ratio_spans):
            continue  # already checked as part of its pair
        if bad_single(s, m.group(2), m.group("key"), sentence[: m.start()]):
            spans.append((s, e))
    for m in _BEFORE.finditer(sentence):
        s, e = m.span(1)
        if any(rs <= s < re_ for rs, re_ in ratio_spans):
            continue
        if bad_single(s, m.group(1), m.group("key"), sentence[:s]):
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
    """Part B (REQ-094) allows no grade value at all: any grade-bound number (digits or spelled
    out, "quattro punti", "Your grade is seven") or any ratio."""
    return any(
        rx.search(text)
        for rx in (_RATIO, _AFTER, _BEFORE, _SPELLED_AFTER, _SPELLED_BEFORE)
    )
