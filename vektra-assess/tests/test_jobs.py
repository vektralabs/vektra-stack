"""Outbox helper (plan assess-job-loop A3): SQL shape, argument rules, no NOTIFY payload."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from vektra_assess import jobs
from vektra_assess.jobs import JobKind, enqueue_job

SRC = Path(jobs.__file__).resolve().parents[0]


class _Savepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Records statements; ``duplicate=True`` makes the INSERT raise the dedupe violation."""

    def __init__(self, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> None:
        sql = str(stmt)
        self.calls.append((sql, params or {}))
        if self.duplicate and sql.startswith("INSERT"):
            raise IntegrityError(
                sql, {}, Exception("violates unique constraint uq_assess_jobs_dedupe")
            )


async def test_insert_then_empty_notify_in_the_same_connection() -> None:
    conn = _FakeConn()
    enqueued = await enqueue_job(
        conn,  # type: ignore[arg-type]
        JobKind.TRANSCRIPT_GENERATE,
        {"session_id": "abc", "epoch": 3},
        dedupe_key="transcript_generate:abc",
    )
    assert enqueued is True
    assert len(conn.calls) == 2
    insert_sql, params = conn.calls[0]
    assert insert_sql.startswith("INSERT INTO assess_jobs")
    assert "ON CONFLICT" not in insert_sql and "RETURNING" not in insert_sql, (
        "the API role cannot SELECT: no ON CONFLICT, no RETURNING"
    )
    assert params["kind"] == "transcript_generate"
    assert params["args"] == '{"epoch":3,"session_id":"abc"}'
    notify_sql, _ = conn.calls[1]
    assert re.fullmatch(r"SELECT pg_notify\('assess_jobs', ''\)", notify_sql)


async def test_deduplicated_job_sends_no_notification() -> None:
    conn = _FakeConn(duplicate=True)
    assert (
        await enqueue_job(conn, JobKind.SETUP_CHECK, {"site_id": "s"}, dedupe_key="k")  # type: ignore[arg-type]
        is False
    )
    assert len(conn.calls) == 1


async def test_other_integrity_errors_are_not_swallowed() -> None:
    class _Other(_FakeConn):
        async def execute(
            self, stmt: Any, params: dict[str, Any] | None = None
        ) -> None:
            raise IntegrityError(
                str(stmt), {}, Exception("violates check ck_assess_jobs_kind")
            )

    with pytest.raises(IntegrityError):
        await enqueue_job(_Other(), JobKind.PURGE)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args",
    [
        {"a": {"nested": 1}},
        {"a": [1]},
        {"a": 1.5},
        {"a": True},
        {"a": None},
        {"a": b"x"},
    ],
)
async def test_arguments_must_be_string_or_integer_ids(args: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="string or an integer id"):
        await enqueue_job(_FakeConn(), JobKind.PURGE, args)  # type: ignore[arg-type]


async def test_unknown_kind_and_bad_attempts_are_rejected() -> None:
    with pytest.raises(ValueError):
        await enqueue_job(_FakeConn(), "preflight")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least 1"):
        await enqueue_job(_FakeConn(), JobKind.PURGE, max_attempts=0)  # type: ignore[arg-type]


def test_no_notify_with_a_payload_anywhere_in_the_package() -> None:
    """INT-002: the channel is always empty. Any ``pg_notify('assess_jobs', <non-empty>)`` or
    ``NOTIFY assess_jobs, ...`` under vektra-assess/src fails here."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        body = path.read_text()
        for m in re.finditer(
            r"pg_notify\(\s*['\"]assess_jobs['\"]\s*,\s*([^)]*)\)", body
        ):
            if m.group(1).strip() not in ("''", '""'):
                offenders.append(f"{path.name}: {m.group(0)}")
        for m in re.finditer(r"NOTIFY\s+assess_jobs\s*,", body):
            offenders.append(f"{path.name}: {m.group(0)}")
    assert not offenders, offenders
