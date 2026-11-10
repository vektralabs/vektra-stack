"""Parser status published by the worker for /health/assess (plan 12b)."""

from __future__ import annotations

from vektra_assess.worker.parser_client import ParserUnavailableError
from vektra_assess.worker.parser_health import ParserHealth


class _Ok:
    async def ping(self, deep: bool = False) -> float:
        return 0.01


class _Down:
    async def ping(self, deep: bool = False) -> float:
        raise ParserUnavailableError("ConnectionRefusedError")


class _Weird:
    async def ping(self, deep: bool = False) -> float:
        raise RuntimeError("socket /run/x/secret-path refused")


async def test_nothing_is_published_before_the_first_ping() -> None:
    assert ParserHealth().stats() == {}


async def test_a_pong_and_a_failure_are_published_by_class_only() -> None:
    h = ParserHealth(clock=lambda: 100.0)
    await h.check(_Ok())
    assert h.stats() == {"parser": {"ok": True, "at": 100.0, "error": None}}
    await h.check(_Down())
    assert h.stats()["parser"] == {
        "ok": False,
        "at": 100.0,
        "error": "ConnectionRefusedError",
    }
    await h.check(_Weird())
    assert h.stats()["parser"]["error"] == "RuntimeError"  # the message never leaves
