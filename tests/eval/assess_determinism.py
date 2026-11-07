"""Determinism run of the assess eval suite (plan assess-llm-runner, task E; REQ-100).

The same items are run ``n`` times (3 by default) through the real runner. Pass criteria:
- Part A (system-rendered) is byte-identical across renders: 100%;
- post-checked values are 100% identical: every grade/mark/point/level value that survives the
  post-check is a snapshot value of the cited item, in every run (the post-check guarantees it;
  this measures that the guarantee holds on delivered output);
- the primary cited label and the behaviour class agree across runs on >= 95% of items (P).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from assess_adapter import Delivered, Fixture, Item, run_item

from vektra_assess.context.part_a import SessionFacts, render_part_a
from vektra_assess.context.references import canonical_labels
from vektra_assess.llm import DiscussionTurnRunner
from vektra_assess.llm.postcheck import citations, values
from vektra_assess.llm.postcheck.sentences import split

AGREEMENT_MIN = 0.95
_TAG = re.compile(r"\[([^\[\]]+)\]")


@dataclass(frozen=True)
class Determinism:
    runs: int
    part_a_identical: float
    values_identical: float
    label_agreement: float
    class_agreement: float

    @property
    def passed(self) -> bool:
        return (
            self.part_a_identical == 1.0
            and self.values_identical == 1.0
            and self.label_agreement >= AGREEMENT_MIN
            and self.class_agreement >= AGREEMENT_MIN
        )


def primary_label(d: Delivered, fx: Fixture) -> str | None:
    labels = canonical_labels(fx.bundle)
    for s in split(d.text, final=True)[0]:
        resolved = citations.check(s, labels).resolved
        if resolved:
            return resolved[0]
    return None


def _values_ok(d: Delivered, fx: Fixture) -> bool:
    index = values.value_index(fx.bundle)
    return all(
        values.check(s, index).replaced == 0 for s in split(d.text, final=True)[0]
    )


def _agreement(xs: Sequence[Sequence[object]]) -> float:
    if not xs:
        return 1.0
    return sum(len(set(x)) == 1 for x in xs) / len(xs)


async def rerun(
    runner: DiscussionTurnRunner,
    fixtures: dict[str, Fixture],
    items: Sequence[Item],
    n: int = 3,
) -> list[list[Delivered]]:
    return [
        [await run_item(runner, fixtures[i.fixture], i) for i in items]
        for _ in range(n)
    ]


def measure(runs: list[list[Delivered]], fixtures: dict[str, Fixture]) -> Determinism:
    n = len(runs)
    part_a = [
        len({render_part_a(f.bundle, SessionFacts(1, 30)).text for _ in range(n)}) == 1
        for f in fixtures.values()
    ]
    per_item = list(zip(*runs, strict=True))
    vals = [
        all(_values_ok(d, fixtures[d.item.fixture]) for d in row) for row in per_item
    ]
    labels = [
        [primary_label(d, fixtures[d.item.fixture]) for d in row] for row in per_item
    ]
    classes = [
        [d.result.behaviour_class if d.result else d.rejected for d in row]
        for row in per_item
    ]
    return Determinism(
        runs=n,
        part_a_identical=sum(part_a) / max(len(part_a), 1),
        values_identical=sum(vals) / max(len(vals), 1),
        label_agreement=_agreement(labels),
        class_agreement=_agreement(classes),
    )
