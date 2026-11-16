"""Review fix 6: the course/instance cache of ``LmsStartSource`` stays bounded.

``gate_facts`` resolves the activity and keeps it for the ``capture`` of the same Start, but a
render check, or a Start refused by a gate, never captures: the cache must not grow with them.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from support.fake_lms import ASSIGN_ACTIVITY, USER, FakeLmsProvider

from vektra_assess.worker.preflight import StartTarget
from vektra_assess.worker.start_source import LmsStartSource


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
            SimpleNamespace(base_url="https://lms.example.org", release="1.0")
        )


@asynccontextmanager
async def acquire():  # type: ignore[no-untyped-def]
    yield _Conn()


async def test_gate_facts_without_capture_never_grows_the_cache_past_its_bound() -> (
    None
):
    clock = [0.0]
    fake = FakeLmsProvider()
    source = LmsStartSource(
        fake,
        parser=None,  # type: ignore[arg-type]
        acquire=acquire,
        resolved_max=8,
        resolved_ttl_s=60,
        clock=lambda: clock[0],
    )
    for _ in range(25):  # render checks: gate facts only, never a capture
        facts = await source.gate_facts(
            StartTarget(
                uuid.uuid4(),
                uuid.uuid4(),
                ASSIGN_ACTIVITY,
                USER,
                10,
                "assign",
                1,
                {},
            )
        )
        assert facts.enrolled is True
    assert len(source._resolved) <= 8
    # every Start re-resolved the activity: a cached ref is never shared across sessions
    assert fake.count("resolve_activity") == 25
    clock[0] += 61  # entries expire after their TTL
    source._resolved.sweep()
    assert len(source._resolved) == 0


async def test_the_material_read_never_uses_the_resolution_cache() -> None:
    """FEAT-028 review 7: TTL 0. A student removed from the course loses the material on the
    next turn, and the material read leaves no ref behind for the enrolment re-check."""
    from support.fake_lms import QUIZ_ACTIVITY

    from vektra_shared.lms import NOT_IN_USER_COURSES, Unreadable, UnreadableReason

    fake = FakeLmsProvider()
    source = LmsStartSource(fake, parser=None, acquire=acquire)  # type: ignore[arg-type]
    target = StartTarget(
        uuid.uuid4(), uuid.uuid4(), QUIZ_ACTIVITY, USER, 10, "quiz", 1, {}
    )
    got = await source.course_material(target)
    assert got is not None
    assert len(source._resolved) == 0  # nothing cached for the enrolment re-check
    fake.fail(
        "resolve_activity",
        Unreadable(UnreadableReason.ACCESS_EXCEPTION, NOT_IN_USER_COURSES),
    )
    assert await source.course_material(target) is None
    assert fake.count("resolve_activity") == 2  # re-resolved on every read
    assert await source.enrolled(target) is False  # and the re-check sees the removal
