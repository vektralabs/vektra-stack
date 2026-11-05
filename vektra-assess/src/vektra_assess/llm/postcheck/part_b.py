"""Part B checks (REQ-094, post-check stage 7).

The opening orientation written by the model carries NO grade, mark, point or level value (any
grade-bound number and any ratio count), at most ``MAX_WORDS`` words and at most ``MAX_LABELS``
distinct labelled items. A violation fails Part B: it is never truncated into a commit (the worker
caller regenerates once, then rolls the start back). Codes are content-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vektra_assess.llm.postcheck.values import asserts_any_value
from vektra_assess.llm.prompts import PART_B_MAX_LABELS as MAX_LABELS
from vektra_assess.llm.prompts import PART_B_MAX_WORDS as MAX_WORDS

VALUE = "part_b_value"
WORDS = "part_b_words"
LABELS = "part_b_labels"
EMPTY = "part_b_empty"

_LABEL = re.compile(
    r"\b(Domanda\s+\w+|Criterio\s+\d+|Valutazione complessiva|Consegna)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"[\[\]]")


def word_count(text: str) -> int:
    """Words as the student reads them: citation brackets are not words of their own."""
    return len(_TAG.sub(" ", text).split())


def labels_named(text: str) -> set[str]:
    return {" ".join(m.group(1).split()).lower() for m in _LABEL.finditer(text)}


@dataclass(frozen=True)
class PartBCheck:
    ok: bool
    violations: tuple[str, ...]
    words: int
    labels: int


def check(text: str) -> PartBCheck:
    words, labels = word_count(text), len(labels_named(text))
    v: list[str] = []
    if not text.strip():
        v.append(EMPTY)
    if asserts_any_value(text):
        v.append(VALUE)
    if words > MAX_WORDS:
        v.append(WORDS)
    if labels > MAX_LABELS:
        v.append(LABELS)
    return PartBCheck(not v, tuple(v), words, labels)
