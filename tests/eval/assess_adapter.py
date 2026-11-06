"""Eval adapter of the assessment discussion runner (plan assess-llm-runner, task E; ADR-0019).

The first Python-level eval adapter in the repo. It never builds a bundle by hand: every seeded
snapshot under ``tests/eval/assess/fixtures/`` goes through ``assemble_context`` with its frozen
policy, exactly as a session does, and every reply is produced by ``DiscussionTurnRunner`` built
by ``vektra_app.assess_llm.build_assess_runner`` with a real ``LLMProvider``. What it scores is
the DELIVERED output (released segments, Part B text), never the raw model text.

Data basis (NFR-022, NFR-023): a dataset whose manifest ``data_basis`` is not ``synthetic`` is
refused BEFORE any request when ``VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud`` (the default): real
student records never reach a cloud provider through the eval suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vektra_assess.config import VektraAssessSettings
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import PolicyInputs, freeze
from vektra_assess.context.snapshot import Snapshot
from vektra_assess.llm import (
    DiscussionTurnRunner,
    PartBRejectedError,
    ReleasedSegment,
    StudentTurn,
    TurnResult,
)
from vektra_assess.llm.types import PriorTurn

ROOT = Path(__file__).resolve().parent / "assess"


class DataBasisRefused(RuntimeError):
    """A non-synthetic dataset was about to be sent to a cloud provider."""


@dataclass(frozen=True)
class Fixture:
    id: str
    bundle: ContextBundle


@dataclass(frozen=True)
class Item:
    """One eval probe: a student turn (with optional history) or a Part B on a fixture."""

    id: str
    fixture: str
    kind: str  # "turn" | "part_b"
    category: str  # e.g. "explain", "refusal:R-SOL", "contest:adversarial", "injection"
    language: str = "it"
    text: str = ""
    history: tuple[tuple[str, str], ...] = ()
    expect: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Delivered:
    item: Item
    text: str
    segments: tuple[str, ...]
    result: TurnResult | None
    rejected: str | None = None  # Part B rejection code


def load_manifest(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def load_fixtures(root: Path = ROOT) -> dict[str, Fixture]:
    out: dict[str, Fixture] = {}
    for path in sorted((root / "fixtures").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        snapshot = Snapshot.model_validate(doc["snapshot"])
        frozen = freeze(PolicyInputs.model_validate(doc["policy_inputs"]))
        out[doc["id"]] = Fixture(doc["id"], assemble_context(snapshot, frozen))
    return out


def load_items(path: Path) -> list[Item]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            d["history"] = tuple(tuple(h) for h in d.get("history", ()))
            items.append(Item(**d))
    return items


def check_data_basis(
    manifest: Mapping[str, Any], settings: VektraAssessSettings
) -> None:
    basis = manifest.get("data_basis")
    if settings.provider_locality == "cloud" and basis != "synthetic":
        raise DataBasisRefused(
            f"data_basis={basis!r} with VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud: refused before "
            "any request; only synthetic data may reach a cloud provider (NFR-022)"
        )


async def run_item(
    runner: DiscussionTurnRunner, fixture: Fixture, item: Item
) -> Delivered:
    if item.kind == "part_b":
        try:
            r = await runner.run_part_b(fixture.bundle, language=item.language)
        except PartBRejectedError as e:
            return Delivered(item, "", (), None, rejected=e.reason)
        return Delivered(item, r.text, (r.text,), r)
    history = tuple(PriorTurn(role, text) for role, text in item.history)  # type: ignore[arg-type]
    segments: list[str] = []
    result: TurnResult | None = None
    async for out in runner.run_stream(
        fixture.bundle, StudentTurn(item.text, len(history), history, item.language)
    ):
        if isinstance(out, ReleasedSegment):
            segments.append(out.text)
        else:
            result = out
    assert result is not None
    return Delivered(item, result.text, tuple(segments), result)


async def run_dataset(
    runner: DiscussionTurnRunner,
    settings: VektraAssessSettings,
    items: Iterable[Item],
    root: Path = ROOT,
) -> list[Delivered]:
    """Every item through the real runner; the data-basis rule is checked first, once."""
    check_data_basis(load_manifest(root), settings)
    fixtures = load_fixtures(root)
    return [await run_item(runner, fixtures[i.fixture], i) for i in items]


def build_runner(
    settings: VektraAssessSettings, **overrides: Any
) -> DiscussionTurnRunner:
    """The production composition (vektra_app), with recording sinks for audit and traces."""
    from vektra_app.assess_llm import build_assess_runner, build_assess_safeguard

    class _Collect:
        def __init__(self) -> None:
            self.rows: list[Any] = []

        async def write(self, trace: Any, namespace: str) -> None:
            self.rows.append((trace, namespace))

        async def __call__(self, event: str, data: Any) -> None:
            self.rows.append((event, data))

    sink = _Collect()
    return build_assess_runner(
        settings,
        safeguard=overrides.pop("safeguard", None)
        or build_assess_safeguard(settings.safeguard_mode),
        trace_writer=overrides.pop("trace_writer", sink),
        audit=overrides.pop("audit", sink),
        **overrides,
    )
