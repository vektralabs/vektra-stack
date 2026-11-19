"""Citation tags (REQ-095, REQ-096b, post-check stage 4).

A citation tag names one of the bundle's canonical labels: ``[Domanda n]``, ``[Criterio n]``,
``[Valutazione complessiva]``, ``[Consegna]`` or ``[Consegna p.X]``. A tag that does not resolve in
the label index is removed and the turn is marked; when an article or determiner governs it
("Non c'è alcuna [Domanda 5] nel contesto") it is replaced by its kind word in lower case
("alcuna domanda"), so the sentence still reads and the number of the non-existent item is gone
(F1 2026-11-19). ``[Materiale: <titolo>]`` (REQ-096 (b),
FEAT-028) resolves only to the title of a course module whose material was retrieved in THIS
turn (``materials``, from the live eligibility read; whitespace and case are not significant):
any other one, a bare ``[Materiale]`` included, is removed and marks the turn, like an unresolved
label. Without course material ``materials`` is empty, so every such tag is removed. Square
brackets that are not citation tags (an interval ``[0.5, 1.5]``, a matrix) are text and left
alone.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

_TAG = re.compile(
    r"\[(?P<body>(?:Domanda|Criterio|Valutazione|Consegna|Materiale|Question|Criterion)\b[^\[\]]*)\]",
    re.IGNORECASE,
)
_PAGE = re.compile(r"^Consegna\s+p\.\s*(\d+)$", re.IGNORECASE)
_MATERIAL = re.compile(r"^Materiale\s*:\s*(?P<title>.+)$", re.IGNORECASE)
MATERIAL_PREFIX = "Materiale: "
# a word that needs a noun after it: removing the tag after one breaks the sentence
_DETERMINER = re.compile(
    r"(?:^|[\s(])(?:alcuna|alcun|nessuna|nessun|una|un|la|il|lo|le|gli|della|del|dello|nella|nel|"
    r"nello|alla|al|allo|dalla|dal|dallo|sulla|sul|sullo|questa|questo|quella|quello|ogni|tale|"
    r"the|a|an|no|this|that|any|each|every)\s+$",
    re.IGNORECASE,
)


def _norm(title: str) -> str:
    return " ".join(title.split()).casefold()


@dataclass(frozen=True)
class CitationCheck:
    text: str
    removed: int  # unresolved tags removed (each marks the turn)
    resolved: tuple[str, ...]  # canonical labels cited, in order


def check(
    sentence: str, labels: Collection[str], materials: Collection[str] = ()
) -> CitationCheck:
    by_lower = {label.lower(): label for label in labels}
    by_title = {_norm(t): t for t in materials}
    removed = 0
    resolved: list[str] = []

    def take(m: re.Match[str]) -> str:
        nonlocal removed
        body = " ".join(m.group("body").split())
        if body.lower().startswith("materiale"):
            found = _MATERIAL.match(body)
            title = by_title.get(_norm(found.group("title"))) if found else None
            if title is None:
                removed += 1
                return ""
            resolved.append(MATERIAL_PREFIX + title)
            return m.group(0)
        label = by_lower.get(body.lower())
        page = _PAGE.match(body)
        if label is None and page and int(page.group(1)) > 0:
            label = by_lower.get("consegna")
        if label is None:
            removed += 1
            if _DETERMINER.search(sentence, 0, m.start()):
                return body.split()[
                    0
                ].lower()  # "domanda", "criterio", "consegna", "question"
            return ""
        resolved.append(label)
        return m.group(0)

    text = _TAG.sub(take, sentence)
    if removed:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([.,;:!?])", r"\1", text).strip()
    return CitationCheck(text, removed, tuple(resolved))
