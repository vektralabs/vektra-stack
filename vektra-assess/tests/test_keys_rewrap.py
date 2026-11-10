"""``python -m vektra_assess.keys rewrap`` and its batch driver (plan assess-hardening-release 13a)."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from vektra_assess import keys_rewrap
from vektra_assess.crypto import rotation


class FakeConn:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _connect(conn: FakeConn) -> Any:
    @asynccontextmanager
    async def cm() -> AsyncIterator[FakeConn]:
        yield conn

    return cm


async def test_each_step_runs_in_committed_batches_until_zero() -> None:
    conn = FakeConn()
    seq_a, seq_b = iter([5, 3, 0]), iter([2, 0])
    order: list[str] = []

    async def a(_c: Any) -> int:
        order.append("a")
        return next(seq_a)

    async def b(_c: Any) -> int:
        order.append("b")
        return next(seq_b)

    assert await rotation.rewrap_to_done(_connect(conn), [a, b]) == 0
    assert order == ["a", "a", "a", "b", "b"] and conn.commits == 5


async def test_a_step_that_makes_no_progress_stops_with_its_remaining_count() -> None:
    conn = FakeConn()
    calls = 0

    async def stuck(_c: Any) -> int:
        nonlocal calls
        calls += 1
        return 4  # a row locked elsewhere: never goes down

    assert await rotation.rewrap_to_done(_connect(conn), [stuck], stall_after=3) == 4
    assert calls == 4  # the first reading, then three without progress


def test_transcripts_rewrap_covers_the_teachers_reasons_too() -> None:
    """The plan 11b gap: rotating the transcript key must also re-encrypt the reasons."""
    key, names = keys_rewrap.WHAT["transcripts"]
    assert key == "assess_transcript_kek"
    assert names == ("rewrap_transcript_deks", "rewrap_teacher_reasons")
    for n in names:
        assert callable(getattr(rotation, n))
    assert len(keys_rewrap.steps_for("transcripts", object(), "t1", 10)) == 2


def test_the_cli_cannot_run_without_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    for k in list(__import__("os").environ):
        if k.startswith("VEKTRA_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VEKTRA_ASSESS_SECRETS_DIR", str(tmp_path))
    out = io.StringIO()
    import asyncio

    code = asyncio.run(keys_rewrap.run("session-keys", "k1", 100, out))
    assert code == 2 and out.getvalue().startswith("cannot run:")


def test_the_cli_refuses_an_out_of_range_batch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        keys_rewrap.main(["--what", "session-keys", "--from-kid", "k1", "--batch", "0"])
        == 2
    )
