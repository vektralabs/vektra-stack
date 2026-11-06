"""Review fix 6: the course/instance cache of ``MoodleStartSource`` stays bounded.

``gate_facts`` resolves the activity and keeps it for the ``capture`` of the same Start, but a
render check, or a Start refused by a gate, never captures: the cache must not grow with them.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from assess_moodle_support import Replay, make_client, manifest, uid

from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.start_source import MoodleStartSource

M = manifest("4.3")


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return []


class _Conn:
    async def execute(self, *_a: Any, **_k: Any) -> _Result:
        return _Result(
            SimpleNamespace(base_url="https://moodle.example.org", release=M["release"])
        )


@asynccontextmanager
async def acquire():  # type: ignore[no-untyped-def]
    yield _Conn()


async def test_gate_facts_without_capture_never_grows_the_cache_past_its_bound() -> (
    None
):
    clock = [0.0]
    source = MoodleStartSource(
        make_client(Replay("4.3")),
        parser=None,  # type: ignore[arg-type]
        acquire=acquire,
        resolved_max=8,
        resolved_ttl_s=60,
        clock=lambda: clock[0],
    )
    cmid = int(M["assigns"]["rubric_vis"]["cmid"])
    for _ in range(25):  # render checks: gate facts only, never a capture
        facts = await source.gate_facts(
            StartTarget(
                uuid.uuid4(),
                uuid.uuid4(),
                cmid,
                uid("4.3", "stu1"),
                10,
                "assign",
                1,
                {},
            )
        )
        assert facts.enrolled is True
    assert len(source._resolved) <= 8
    clock[0] += 61  # entries expire after their TTL
    source._resolved.sweep()
    assert len(source._resolved) == 0
