"""Server-side item references (REQ-095): labels are snapshot-local, never LMS ids.

``resolve_reference(bundle, ref, source=...)``:
- ``Resolved(label)`` for a canonical label ("Domanda 3", "D3", "3", "Criterio 2", "C2",
  "Valutazione complessiva", "Consegna"), an ordinal ("la terza", "il secondo criterio") or a
  keyword set that matches exactly one item's question text;
- ``Ambiguous(candidates)`` with at most 3 candidates: the bot asks, it never guesses;
- ``NotFound(text, labels)`` with the fixed text and the valid list, for free text;
- ``Rejected(reason)`` for anything shaped like a LMS id (``slot``, ``cmid``, ``attemptid``,
  a bare number that is not a label...) and, from the picker, for a label outside the snapshot.
  The caller audits it as ``assess_item_ref_rejected`` with ``audit_data()`` (reason only).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from vektra_assess.context import strings as S
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.snapshot import ActivityKind

MAX_CANDIDATES = 3

_ID_KEYS = re.compile(
    r"\b(slot|cmid|attempt_?id|question_?id|quiz_?id|assign_?id|user_?id|course_?id|instance_?id|"
    r"context_?id|id)\s*[:=#]",
    re.I,
)
_ID_WORDS = re.compile(
    r"\b(slot|cmid|attemptid|questionid|quizid|assignid|userid|courseid)\b", re.I
)

_ORDINALS = {
    "prima": 1, "primo": 1, "seconda": 2, "secondo": 2, "terza": 3, "terzo": 3,
    "quarta": 4, "quarto": 4, "quinta": 5, "quinto": 5, "sesta": 6, "sesto": 6,
    "settima": 7, "settimo": 7, "ottava": 8, "ottavo": 8, "nona": 9, "nono": 9,
    "decima": 10, "decimo": 10, "ultima": -1, "ultimo": -1,
}  # fmt: skip
_STOP = frozenset(
    "il lo la i gli le un uno una di del della dei delle quella quello quelle quelli sulla "
    "sullo sul sui sulle su che e a da in con per tra fra domanda criterio".split()
)


@dataclass(frozen=True)
class Resolved:
    label: (
        str  # the canonical label: "Domanda 3", "Criterio 2", "Valutazione complessiva"
    )


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[str, ...]  # at most MAX_CANDIDATES canonical labels

    @property
    def text(self) -> str:
        return S.REF_AMBIGUOUS.format(candidates=", ".join(self.candidates))


@dataclass(frozen=True)
class NotFound:
    text: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    reason: Literal["lms_id", "out_of_snapshot", "empty"]

    def audit_data(self) -> dict[str, str]:
        return {"reason_code": self.reason}


type Resolution = Resolved | Ambiguous | NotFound | Rejected


_ARTICLE = re.compile(r"^(il|lo|la|i|gli|le|l|un|una|uno)\s+")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"['\u2019`]", " ", t)  # "l'ultima" -> "l ultima"
    return re.sub(r"\s+", " ", t).strip()


def _strip_article(text: str) -> str:
    return _ARTICLE.sub("", text)


def _stem(word: str) -> str:
    """Crude Italian singular/plural folding: "matrici" and "matrice" both give "matric"."""
    return word[:-1] if len(word) > 4 and word[-1] in "aeio" else word


def canonical_labels(bundle: ContextBundle) -> tuple[str, ...]:
    out = [S.QUIZ_LABEL.format(label=i.label) for i in bundle.quiz_items]
    out += [
        S.CRITERION_LABEL.format(n=n) for n, _ in enumerate(bundle.criteria, start=1)
    ]
    out.append(S.OVERALL_LABEL)
    if bundle.activity_kind is ActivityKind.ASSIGN:
        out.append(S.SUBMISSION_LABEL)
    return tuple(out)


def _direct(bundle: ContextBundle, ref: str) -> str | None:
    """A canonical label or one of its short forms."""
    by_norm: dict[str, str] = {}
    for i in bundle.quiz_items:
        full = S.QUIZ_LABEL.format(label=i.label)
        for form in (
            full,
            f"d{i.label}",
            f"d {i.label}",
            f"q{i.label}",
            f"domanda n. {i.label}",
        ):
            by_norm[_norm(form)] = full
        if bundle.activity_kind is ActivityKind.QUIZ:
            by_norm[_norm(i.label)] = full
    for n, _ in enumerate(bundle.criteria, start=1):
        full = S.CRITERION_LABEL.format(n=n)
        for form in (full, f"c{n}", f"c {n}"):
            by_norm[_norm(form)] = full
        if bundle.activity_kind is ActivityKind.ASSIGN:
            by_norm[_norm(str(n))] = full
    by_norm[_norm(S.OVERALL_LABEL)] = S.OVERALL_LABEL
    by_norm[_norm("voto")] = S.OVERALL_LABEL
    if bundle.activity_kind is ActivityKind.ASSIGN:
        by_norm[_norm(S.SUBMISSION_LABEL)] = S.SUBMISSION_LABEL
    key = _norm(ref)
    return by_norm.get(key) or by_norm.get(_strip_article(key))


def _ordinal(bundle: ContextBundle, ref: str) -> Resolution | None:
    words = _norm(ref).split()
    n = next((_ORDINALS[w] for w in words if w in _ORDINALS), None)
    if n is None:
        return None
    wants_q = "domanda" in words
    wants_c = "criterio" in words
    quiz = [S.QUIZ_LABEL.format(label=i.label) for i in bundle.quiz_items]
    crit = [
        S.CRITERION_LABEL.format(n=k) for k, _ in enumerate(bundle.criteria, start=1)
    ]
    pools = [
        p
        for p, want in ((quiz, wants_q), (crit, wants_c))
        if p and (want or not (wants_q or wants_c))
    ]
    hits = []
    for pool in pools:
        idx = len(pool) - 1 if n == -1 else n - 1
        if 0 <= idx < len(pool):
            hits.append(pool[idx])
    if len(hits) == 1:
        return Resolved(hits[0])
    if len(hits) > 1:
        return Ambiguous(tuple(hits[:MAX_CANDIDATES]))
    return None


def _keywords(bundle: ContextBundle, ref: str) -> Resolution | None:
    words = {
        _stem(w)
        for w in re.findall(r"\w+", _norm(ref))
        if len(w) > 2 and w not in _STOP
    }
    if not words:
        return None
    scored = []
    for i in bundle.quiz_items:
        text = {_stem(w) for w in re.findall(r"\w+", _norm(i.question_text or ""))}
        score = len(words & text)
        if score:
            scored.append((score, S.QUIZ_LABEL.format(label=i.label)))
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [lab for s, lab in scored if s == best]
    if len(top) == 1:
        return Resolved(top[0])
    return Ambiguous(tuple(top[:MAX_CANDIDATES]))


def resolve_reference(
    bundle: ContextBundle, ref: str, *, source: Literal["text", "picker"] = "text"
) -> Resolution:
    if not ref or not ref.strip():
        return Rejected("empty")
    if _ID_KEYS.search(ref) or _ID_WORDS.search(ref):
        return Rejected("lms_id")
    direct = _direct(bundle, ref)
    if direct is not None:
        return Resolved(direct)
    if source == "picker":
        # the picker only ever offers snapshot labels: anything else is forged
        return Rejected("out_of_snapshot")
    if re.fullmatch(r"\s*#?\d+\s*", ref):
        return Rejected("lms_id")  # a bare number that is not a label looks like an id
    for step in (_ordinal, _keywords):
        res = step(bundle, ref)
        if res is not None:
            return res
    labels = canonical_labels(bundle)
    return NotFound(
        S.REF_NOT_FOUND.format(label=ref.strip(), labels=", ".join(labels)), labels
    )
