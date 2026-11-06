"""NFR-023 activations on a real database (plan assess-llm-runner, task E): the endpoint as the
API role, append-only rows, every attempt audited, the read used by startup."""

from __future__ import annotations

import pytest
from fakes import passing_report_body

from vektra_assess.llm.activation import is_activated, report_sha256

from .conftest import Pg
from .test_setup_check_api import auth, err, world_ctx

pytestmark = pytest.mark.integration


def _report(**over: object) -> dict[str, object]:
    doc: dict[str, object] = passing_report_body(**{"model": "ollama/act", **over})
    doc["report_sha256"] = report_sha256(doc)
    return doc


async def _count(pg: Pg, sql: str) -> int:
    return int((await pg.run("super", sql))[0][0])


async def test_a_passing_report_is_activated_and_audited(pg: Pg) -> None:
    async with world_ctx(pg) as (client, _a, _b):
        before = await _count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_model_activation_accepted'",
        )
        r = await client.post(
            "/api/v1/assess/admin/model-activations",
            json=_report(),
            headers=auth("admin"),
        )
        assert r.status_code == 201, r.text
        assert r.json()["prompt_version"] == "abcd1234"
        rows = await pg.run(
            "super",
            "SELECT model, prompt_version, hard_pass, activated_by_key_id IS NOT NULL FROM assess_model_activations",
        )
        assert ("ollama/act", "abcd1234", True, True) in [tuple(x) for x in rows]
        after = await _count(
            pg,
            "SELECT count(*) FROM assess_audit_events WHERE event = 'assess_model_activation_accepted'",
        )
        assert after == before + 1  # exactly one audit row for this attempt
        async with pg.conn("api") as conn:
            assert await is_activated(conn, "ollama/act", "abcd1234")
            assert not await is_activated(conn, "ollama/act", "ffff0000")


async def test_a_failing_report_is_refused_audited_and_not_stored(pg: Pg) -> None:
    async with world_ctx(pg) as (client, _a, _b):
        n_rows = await _count(pg, "SELECT count(*) FROM assess_model_activations")
        bad = _report(hard={"verdict": 2}, status="FAIL", model="ollama/bad")
        r = await client.post(
            "/api/v1/assess/admin/model-activations", json=bad, headers=auth("admin")
        )
        assert r.status_code == 422 and err(r)["code"] == "ERR-ASSESS-004"
        assert "hard_class_failed" in err(r)["remediation"]
        assert (
            await _count(pg, "SELECT count(*) FROM assess_model_activations") == n_rows
        )
        refused = await pg.run(
            "super",
            "SELECT reason_code, data->>'model' FROM assess_audit_events "
            "WHERE event = 'assess_model_activation_refused' ORDER BY id DESC LIMIT 1",
        )
        assert refused[0][1] == "ollama/bad" and "hard_class_failed" in refused[0][0]


async def test_only_an_exact_admin_key_may_activate(pg: Pg) -> None:
    async with world_ctx(pg) as (client, _a, _b):
        for who in ("teacher_a", "query"):
            r = await client.post(
                "/api/v1/assess/admin/model-activations",
                json=_report(),
                headers=auth(who),
            )
            assert r.status_code == 403, (who, r.text)


async def test_activations_are_append_only_for_every_role(pg: Pg) -> None:
    from sqlalchemy.exc import DBAPIError

    for role in ("api", "worker"):
        for sql in (
            "UPDATE assess_model_activations SET hard_pass = true",
            "DELETE FROM assess_model_activations",
        ):
            with pytest.raises(DBAPIError):
                await pg.run(role, sql)
    async with pg.conn("worker") as conn:  # the worker reads (Part B pre-flight)
        assert await is_activated(conn, "nobody/none", "00000000") is False
