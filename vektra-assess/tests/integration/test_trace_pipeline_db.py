"""query_traces.pipeline and the reserved assess-discussion namespace (plan 7, C; formerly 0019,
now the assess base revision).

Real Postgres: the column (the platform's, revision 0009) and the seed row are counted after
upgrade, an assess trace round-trips with its pipeline, and the assess downgrade removes the
assess traces and the namespace, and keeps the column.
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
#: as _INSERT, with a chosen duration and retrieved chunk scores (the inputs of p95 and the
#: average retrieval score)
_INSERT_TIMED = (
    "INSERT INTO query_traces (response_id, namespace_id, total_duration_ms, chunks_retrieved, "
    "llm_model, prompt_version, pipeline) VALUES (:rid, :ns, :ms, CAST(:chunks AS jsonb), 'm', "
    "'abcd1234', :pipeline)"
)


def _timed(ns: str, pipeline: str | None, ms: int, score: float) -> dict[str, object]:
    import json

    return {
        "rid": uuid.uuid4(),
        "ns": ns,
        "pipeline": pipeline,
        "ms": ms,
        "chunks": json.dumps([{"chunk_id": f"c-{ms}", "score": score}]),
    }


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
                    m.p95_latency_ms,
                    m.avg_retrieval_score,
                    dict(m.model_distribution),
                    sorted(str(t.response_id) for t in listed),
                )

        # RAG traces with distinct durations and non-empty chunk scores, so p95 and the average
        # retrieval score are real numbers that an unfiltered assess row would move
        for ms, score in ((10, 0.4), (30, 0.8)):
            await pg.run("super", _INSERT_TIMED, _timed("default", None, ms, score))
        before = await snapshot()
        assert (
            before[2] > 0 and before[3] > 0
        )  # p95 and avg score are not the empty 0.0
        # an assess trace far outside both distributions: a missing RAG filter shows up
        assess_id = uuid.uuid4()
        assess = _timed("assess-discussion", "assess-discussion", 100_000, 0.99)
        await pg.run("super", _INSERT_TIMED, {**assess, "rid": assess_id})
        total = await pg.run(
            "super", "SELECT count(*) FROM query_traces WHERE pipeline IS NOT NULL"
        )
        assert total[0][0] >= 1  # the assess rows are really there
        assert await snapshot() == before
        async with AsyncSession(engine) as session:
            assert await svc.get_trace(session, assess_id) is None
    finally:
        await engine.dispose()


async def test_downgrade_removes_assess_traces_and_namespace_and_keeps_the_column(
    pg: Pg,
) -> None:
    """The column is the platform's (revision 0009): the assess downgrade keeps it."""
    rag_id = uuid.uuid4()
    await pg.run("super", _INSERT, {"rid": rag_id, "ns": "default", "pipeline": None})
    await pg.run(
        "super",
        _INSERT,
        {
            "rid": uuid.uuid4(),
            "ns": "assess-discussion",
            "pipeline": "assess-discussion",
        },
    )
    res = _alembic(pg.super_url, "downgrade", "assess@base")
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
        assert cols == [(1,)]
        # only the assess traces go: a RAG trace survives the downgrade
        assert await pg.run(
            "super",
            "SELECT count(*) FROM query_traces WHERE response_id = :r",
            {"r": rag_id},
        ) == [(1,)]
    finally:
        res = _alembic(pg.super_url, "upgrade", "heads")
        assert res.returncode == 0, res.stderr


_NS_ROW = (
    "SELECT display_name, config::text FROM namespaces WHERE id = 'assess-discussion'"
)


async def _revision(pg: Pg) -> str:
    rows = await pg.run("super", "SELECT version_num FROM alembic_version")
    return ",".join(sorted(str(r[0]) for r in rows))


async def _restore_head(pg: Pg) -> None:
    """Cleanup that works whatever assertion failed: the test-owned row is removed (with the
    assess branch down, any ``assess-discussion`` row is the test's, the reserved one went with
    the downgrade) or, if
    the downgrade never ran, the reserved marker is put back; then the schema returns to head."""
    if await _revision(pg) == "0009":
        await pg.run("super", "DELETE FROM namespaces WHERE id = 'assess-discussion'")
    else:
        await pg.run(
            "super",
            "UPDATE namespaces SET config = '{\"reserved\": true}'::jsonb "
            "WHERE id = 'assess-discussion'",
        )
    res = _alembic(pg.super_url, "upgrade", "heads")
    assert res.returncode == 0, res.stderr


async def test_upgrade_refuses_a_pre_existing_namespace_and_leaves_it_untouched(
    pg: Pg,
) -> None:
    """A deployment's own ``assess-discussion`` namespace is never adopted as the reserved one
    (it would be frozen read-only) and never deleted by a later downgrade: the upgrade stops."""
    res = _alembic(pg.super_url, "downgrade", "assess@base")
    assert res.returncode == 0, res.stderr
    try:
        await pg.run(
            "super",
            "INSERT INTO namespaces (id, display_name, config) "
            "VALUES ('assess-discussion', 'a user namespace', '{\"top_k\": 3}'::jsonb)",
        )
        res = _alembic(pg.super_url, "upgrade", "heads")
        assert res.returncode != 0
        assert "reserves the namespace id 'assess-discussion'" in res.stderr
        assert (
            "re-ingest its documents into a namespace with another name" in res.stderr
        )
        # nothing of the run was applied, and the user's row is exactly as it was
        assert await _revision(pg) == "0009"
        assert await pg.run("super", _NS_ROW) == [("a user namespace", '{"top_k": 3}')]
        cols = await pg.run(
            "super",
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'query_traces' AND column_name = 'pipeline'",
        )
        assert cols == [(1,)]  # the platform's column, untouched by the refused run
    finally:
        # the documented way out (delete the row, upgrade again), and the cleanup too
        await _restore_head(pg)
    assert await pg.run("super", _NS_ROW) == [
        ("Assess discussion traces (reserved)", '{"reserved": true}')
    ]


@pytest.mark.parametrize(
    "config", ["{}", '{"reserved": false}'], ids=["no-marker", "marker-false"]
)
async def test_downgrade_deletes_only_the_reserved_row_the_upgrade_created(
    pg: Pg, config: str
) -> None:
    """The marker is the downgrade's second lock: a row without it, or with any value but
    ``true``, is not deleted (and the next upgrade then refuses it, as above)."""
    await pg.run(
        "super",
        "UPDATE namespaces SET config = CAST(:c AS jsonb) WHERE id = 'assess-discussion'",
        {"c": config},
    )
    try:
        res = _alembic(pg.super_url, "downgrade", "assess@base")
        assert res.returncode == 0, res.stderr
        assert await pg.run("super", _NS_ROW) == [
            ("Assess discussion traces (reserved)", config)
        ]
        res = _alembic(pg.super_url, "upgrade", "heads")
        assert res.returncode != 0
        assert await _revision(pg) == "0009"
    finally:
        await _restore_head(pg)
    assert await pg.run("super", _NS_ROW) == [
        ("Assess discussion traces (reserved)", '{"reserved": true}')
    ]


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
