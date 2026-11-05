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
