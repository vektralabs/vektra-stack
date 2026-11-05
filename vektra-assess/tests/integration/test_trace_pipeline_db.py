"""Revision 0019: query_traces.pipeline and the reserved assess-discussion namespace (plan 7, C).

Real Postgres: the column and the seed row are counted after upgrade, an assess trace round-trips
with its pipeline, and the downgrade removes the assess traces, the namespace and the column.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import Pg, _alembic

pytestmark = pytest.mark.integration

_INSERT = (
    "INSERT INTO query_traces (response_id, namespace_id, total_duration_ms, llm_model, "
    "prompt_version, pipeline) VALUES (:rid, :ns, 5, 'm', 'abcd1234', :pipeline)"
)


async def test_upgrade_adds_the_column_and_the_reserved_namespace(pg: Pg) -> None:
    cols = await pg.run(
        "super",
        "SELECT data_type, is_nullable, character_maximum_length FROM information_schema.columns "
        "WHERE table_name = 'query_traces' AND column_name = 'pipeline'",
    )
    assert [tuple(c) for c in cols] == [("character varying", "YES", 32)]
    ns = await pg.run(
        "super",
        "SELECT config->>'reserved' FROM namespaces WHERE id = 'assess-discussion'",
    )
    assert [tuple(r) for r in ns] == [("true",)]


async def test_an_assess_trace_round_trips_and_rag_traces_stay_null(pg: Pg) -> None:
    a, r = uuid.uuid4(), uuid.uuid4()
    await pg.run(
        "super",
        _INSERT,
        {"rid": a, "ns": "assess-discussion", "pipeline": "assess-discussion"},
    )
    await pg.run("super", _INSERT, {"rid": r, "ns": "default", "pipeline": None})
    rows = await pg.run(
        "super",
        "SELECT response_id, pipeline FROM query_traces WHERE response_id IN (:a, :r)",
        {"a": a, "r": r},
    )
    assert dict((row[0], row[1]) for row in rows) == {a: "assess-discussion", r: None}


async def test_assess_traces_do_not_move_the_rag_analytics(pg: Pg) -> None:
    """Rows are counted before and after: an assess trace changes no RAG metric or listing."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from vektra_analytics.service import AnalyticsService

    svc = AnalyticsService()
    engine = create_async_engine(pg.super_url)
    try:

        async def snapshot() -> tuple[object, ...]:
            async with AsyncSession(engine) as session:
                m = await svc.get_metrics(session)
                listed = await svc.list_traces(session, limit=1000)
                return (
                    m.total_queries,
                    m.avg_latency_ms,
                    dict(m.model_distribution),
                    sorted(str(t.response_id) for t in listed),
                )

        await pg.run(
            "super", _INSERT, {"rid": uuid.uuid4(), "ns": "default", "pipeline": None}
        )
        before = await snapshot()
        assess_id = uuid.uuid4()
        await pg.run(
            "super",
            _INSERT,
            {
                "rid": assess_id,
                "ns": "assess-discussion",
                "pipeline": "assess-discussion",
            },
        )
        total = await pg.run(
            "super", "SELECT count(*) FROM query_traces WHERE pipeline IS NOT NULL"
        )
        assert total[0][0] >= 1  # the assess rows are really there
        assert await snapshot() == before
        async with AsyncSession(engine) as session:
            assert await svc.get_trace(session, assess_id) is None
    finally:
        await engine.dispose()


async def test_downgrade_removes_assess_traces_namespace_and_column(pg: Pg) -> None:
    await pg.run(
        "super",
        _INSERT,
        {
            "rid": uuid.uuid4(),
            "ns": "assess-discussion",
            "pipeline": "assess-discussion",
        },
    )
    res = _alembic(pg.super_url, "downgrade", "0018")
    assert res.returncode == 0, res.stderr
    try:
        assert await pg.run(
            "super", "SELECT count(*) FROM namespaces WHERE id = 'assess-discussion'"
        ) == [(0,)]
        cols = await pg.run(
            "super",
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'query_traces' AND column_name = 'pipeline'",
        )
        assert cols == [(0,)]
    finally:
        res = _alembic(pg.super_url, "upgrade", "head")
        assert res.returncode == 0, res.stderr


# --- revision 0020: the worker writes Part B traces, and only those --------------------------------


async def test_the_worker_can_insert_an_assess_trace_and_nothing_else(pg: Pg) -> None:
    from datetime import UTC, datetime

    from vektra_app.assess_llm import WorkerTraceWriter
    from vektra_shared.types import QueryTrace, StepTrace

    rid = uuid.uuid4()
    trace = QueryTrace(
        rid, [StepTrace("llm_call", 3, {"attempt": 1})], 3, [], "m", "abcd1234",
        datetime.now(UTC), "assess-discussion",
    )  # fmt: skip
    await WorkerTraceWriter(lambda: pg.conn("worker")).write(trace, "assess-discussion")
    rows = await pg.run(
        "super", "SELECT pipeline, steps->0->>'name' FROM query_traces WHERE response_id = :r",
        {"r": rid},
    )  # fmt: skip
    assert [tuple(r) for r in rows] == [("assess-discussion", "llm_call")]

    # the database, not the writer, refuses a RAG-looking row and any read
    from sqlalchemy.exc import DBAPIError

    for ns, pipeline in (
        ("default", None),
        ("default", "assess-discussion"),
        ("assess-discussion", None),
    ):
        with pytest.raises(DBAPIError):
            await pg.run(
                "worker", _INSERT, {"rid": uuid.uuid4(), "ns": ns, "pipeline": pipeline}
            )
    with pytest.raises(DBAPIError):
        await pg.run("worker", "SELECT 1 FROM query_traces")
    with pytest.raises(DBAPIError):
        await pg.run(
            "api",
            _INSERT,
            {
                "rid": uuid.uuid4(),
                "ns": "assess-discussion",
                "pipeline": "assess-discussion",
            },
        )
