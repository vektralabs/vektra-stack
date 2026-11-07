"""The test doubles themselves (plan assess-llm-runner, task D): a fake that cannot fail proves nothing."""

from __future__ import annotations

import pytest
from fakes import FORBIDDEN, HOSTILE, ScriptedHostileLLM, TimedLLM


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_every_hostile_reply_carries_a_forbidden_pattern(name: str) -> None:
    assert any(f in HOSTILE[name] for f in FORBIDDEN), name


def test_every_forbidden_pattern_is_exercised() -> None:
    assert all(any(f in r for r in HOSTILE.values()) for f in FORBIDDEN)


async def test_the_hostile_provider_streams_all_of_them() -> None:
    llm = ScriptedHostileLLM()
    it = await llm.stream([], "m")
    text = "".join([c.content async for c in it])
    assert all(r in text for r in HOSTILE.values())


async def test_timed_llm_fails_on_demand() -> None:
    with pytest.raises(ConnectionError):
        await TimedLLM([], fail="open").stream([], "m")
