"""Part B checks (REQ-094, post-check stage 7).

The opening orientation written by the model carries NO grade, mark, point or level value (any
grade-bound number and any ratio count), at most ``MAX_WORDS`` words and at most ``MAX_LABELS``
distinct labelled items. A violation fails Part B: it is never truncated into a commit (the worker
caller regenerates once, then rolls the start back). Codes are content-free.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from vektra_assess.llm import prompts
from vektra_assess.llm.postcheck.values import asserts_any_value

MAX_WORDS = prompts.PART_B_MAX_WORDS
MAX_LABELS = prompts.PART_B_MAX_LABELS

VALUE = "part_b_value"
WORDS = "part_b_words"
LABELS = "part_b_labels"
EMPTY = "part_b_empty"
# set by the runner around ``check``: the other stages, applied to Part B as a whole
MARKER = "part_b_marker"
UNRESOLVED = "part_b_unresolved_tag"
LEXICON = "part_b_lexicon"
QUOTE = "part_b_quote"
SAFEGUARD = "part_b_safeguard"

_LABEL = re.compile(
    r"\b(Domanda\s+\w+|Criterio\s+\d+|Valutazione complessiva|Consegna)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"[\[\]]")


def word_count(text: str) -> int:
    """Words as the student reads them: citation brackets are not words of their own."""
    return len(_TAG.sub(" ", text).split())


def labels_named(text: str, known: Collection[str] | None = None) -> set[str]:
    """The distinct items the text names. With ``known`` (the bundle's canonical labels) only
    those count: "scegli la domanda che preferisci" or "rileggi la consegna" in a quiz name no
    item, and counting them rolled starts back (psico E2E 2026-11-15, 2 of 27 starts)."""
    names = {" ".join(m.group(1).split()).lower() for m in _LABEL.finditer(text)}
    if known is None:
        return names
    keep = {k.lower() for k in known}
    return {n for n in names if n in keep}


@dataclass(frozen=True)
class PartBCheck:
    ok: bool
    violations: tuple[str, ...]
    words: int
    labels: int


def check(text: str, known: Collection[str] | None = None) -> PartBCheck:
    words, labels = word_count(text), len(labels_named(text, known))
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
