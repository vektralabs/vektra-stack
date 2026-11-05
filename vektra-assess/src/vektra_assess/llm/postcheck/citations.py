"""Citation tags (REQ-095, REQ-096b, post-check stage 4).

A citation tag names one of the bundle's canonical labels: ``[Domanda n]``, ``[Criterio n]``,
``[Valutazione complessiva]``, ``[Consegna]`` or ``[Consegna p.X]``. A tag that does not resolve in
the label index is removed and the turn is marked. ``[Materiale: ...]`` is always removed in v1: RAG
is OFF, so there is no material it could cite. Square brackets that are not citation tags (an
interval ``[0.5, 1.5]``, a matrix) are text and left alone.
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


@dataclass(frozen=True)
class CitationCheck:
    text: str
    removed: int  # unresolved tags removed (each marks the turn)
    resolved: tuple[str, ...]  # canonical labels cited, in order


def check(sentence: str, labels: Collection[str]) -> CitationCheck:
    by_lower = {label.lower(): label for label in labels}
    removed = 0
    resolved: list[str] = []

    def take(m: re.Match[str]) -> str:
        nonlocal removed
        body = " ".join(m.group("body").split())
        label = by_lower.get(body.lower())
        page = _PAGE.match(body)
        if label is None and page and int(page.group(1)) > 0:
            label = by_lower.get("consegna")
        if label is None or body.lower().startswith("materiale"):
            removed += 1
            return ""
        resolved.append(label)
        return m.group(0)

    text = _TAG.sub(take, sentence)
    if removed:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([.,;:!?])", r"\1", text).strip()
    return CitationCheck(text, removed, tuple(resolved))
