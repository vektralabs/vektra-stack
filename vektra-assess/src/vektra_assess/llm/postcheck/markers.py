"""Machine markers in the model output (plan assess-llm-runner, post-check stage 1).

The model is told to open a behaviour-class reply with ``[[CLASS <code>]]`` and to record a contested
point with ``[[PUNTO_CONTESTATO label="<label>" status="proposed|confirmed|corrected"]]``. Markers are
parsed and stripped here and never reach the student. A marker with an unknown class, an unknown
status or a label that is not one of the bundle's canonical labels is dropped; the caller audits
the returned content-free code (never the label text the model wrote).

A recognised opener with no closing ``]]`` (``[[CLASS R-SOL`` cut by the splitter or by the end of
the stream) is never released: it is removed up to its first ``]`` (or to the end of the sentence)
and dropped as ``marker_unterminated``, which also fails Part B.

Only these two marker names are recognised: any other ``[[...]]`` (a matrix such as
``[[1, 2], [3, 4]]``) is ordinary text and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from vektra_assess.llm.types import ContestEntry, ContestStatus

CLASS_CODES = frozenset(
    {"R-SOL", "R-OTHER", "R-OFFTOPIC", "R-INJECT", "R-GHOST", "R-GRADE", "W-DISTRESS"}
)
_STATUSES: frozenset[ContestStatus] = frozenset({"proposed", "confirmed", "corrected"})
_MARKER = re.compile(r"\[\[\s*(CLASS|PUNTO_CONTESTATO)\b(.*?)\]\]", re.DOTALL)
# what is left of a recognised opener after every complete marker is removed
_UNTERMINATED = re.compile(r"\[\[\s*(?:CLASS|PUNTO_CONTESTATO)\b[^\]]*\]?", re.DOTALL)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# content-free codes the caller audits
CLASS_UNKNOWN = "marker_class_unknown"
CONTEST_LABEL_UNKNOWN = "marker_contest_label_unknown"
CONTEST_MALFORMED = "marker_contest_malformed"
UNTERMINATED = "marker_unterminated"


@dataclass(frozen=True)
class Markers:
    text: str  # the sentence with every recognised marker removed
    behaviour_class: str | None
    contest: tuple[ContestEntry, ...]
    dropped: tuple[str, ...]  # codes of markers dropped as invalid


def extract(sentence: str, labels: Collection[str]) -> Markers:
    klass: str | None = None
    contest: list[ContestEntry] = []
    dropped: list[str] = []

    def take(m: re.Match[str]) -> str:
        nonlocal klass
        name, body = m.group(1), m.group(2).strip()
        if name == "CLASS":
            if body in CLASS_CODES and klass is None:
                klass = body
            elif body not in CLASS_CODES:
                dropped.append(CLASS_UNKNOWN)
            return ""
        attrs = dict(_ATTR.findall(body))
        label, status = attrs.get("label"), attrs.get("status")
        if label is None or status not in _STATUSES:
            dropped.append(CONTEST_MALFORMED)
        elif label not in labels:
            dropped.append(CONTEST_LABEL_UNKNOWN)
        else:
            contest.append(ContestEntry(label=label, status=status))
        return ""

    def cut(_m: re.Match[str]) -> str:
        dropped.append(UNTERMINATED)
        return ""

    text = _UNTERMINATED.sub(cut, _MARKER.sub(take, sentence))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return Markers(text, klass, tuple(contest), tuple(dropped))
