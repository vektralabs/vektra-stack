"""Read back one sealed teacher-config field, "Mostra" (FEAT-030, OQ-034 resolved 2026-11-18).

Real Postgres, the restricted API role through the real route, the worker side as
``vektra_assess_worker``. The API holds no private key: it queues a ``config_reveal`` job with an
ephemeral public key, the worker re-seals the field to it in ``assess_config_reveals`` (ciphertext
only), and the API opens it in memory and deletes the row in the same transaction as the audit.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from support.recipients import config_pair
from support.world import auth, default_settings, err, make_key, world

from integration.test_teacher_authz import COURSE, EDITOR, OTHER, seed_gate
from vektra_assess.api import config_reveal as reveal_api
from vektra_assess.api import teacher_config as route
from vektra_assess.crypto.reveal import EphemeralKey
from vektra_assess.crypto.teacher_config import (
    PASTED_RUBRIC,
    SOLUTION,
    TRANSCRIPT_RECIPIENTS,
    store_sealed_field,
)
from vektra_assess.worker.config_reveal import (
    reveal_field,
    sweep_config_reveals,
)

pytestmark = pytest.mark.integration

TEXT = "Soluzione attesa: 42 (canary-7f3a)"
RUBRIC = "Criterio A: 3 punti (canary-91bc)"
A, B = "prof.uno@uni.example", "prof.due@uni.example"


def url(cmid: int) -> str:
    return f"/api/v1/assess/activities/{cmid}/config/reveal"


def body(field: Any = SOLUTION, **over: Any) -> dict[str, Any]:
    return {
        "actor_userid": EDITOR,
        "courseid": COURSE,
        "activity_type": "assign",
        "field": field,
        **over,
    }


def _acquire(engine: Any) -> Any:
    @asynccontextmanager
    async def acquire():  # type: ignore[no-untyped-def]
        async with engine.connect() as conn:
            yield conn

    return acquire


@pytest.fixture
def pair():  # type: ignore[no-untyped-def]
    return config_pair()


@asynccontextmanager
async def ready(pg, pair, cmid: int = 70, timeout_s: float = 3.0):  # type: ignore[no-untyped-def]
    settings = default_settings(VEKTRA_ASSESS_CONFIG_REVEAL_TIMEOUT_S=timeout_s)
    async with world(pg, settings) as w:
        w.app.state.assess_keys[route.PUBLIC_KEY] = pair[0]
        await seed_gate(pg, w.site, cmid)
        yield w


async def seal_fields(pg, pair, site: uuid.UUID, cmid: int = 70) -> None:  # type: ignore[no-untyped-def]
    await pg.run(
        "super",
        "INSERT INTO assess_activity_config (site_id, cmid, activity_type) "
        "VALUES (:s, :c, 'assign') ON CONFLICT DO NOTHING",
        {"s": site, "c": cmid},
    )
    async with pg.conn("api", site=site) as conn:
        await store_sealed_field(
            conn, pair[0], site, cmid, SOLUTION, TEXT, actor_userid=1
        )
        await store_sealed_field(
            conn, pair[0], site, cmid, PASTED_RUBRIC, RUBRIC, actor_userid=1
        )
        await store_sealed_field(
            conn,
            pair[0],
            site,
            cmid,
            TRANSCRIPT_RECIPIENTS,
            json.dumps([A, B]),
            actor_userid=1,
            refs=["r1", "r2"],
            item_count=2,
        )
        await conn.commit()


async def serve_one(pg, pair, seen: list[dict[str, Any]] | None = None) -> None:  # type: ignore[no-untyped-def]
    """The worker side: take the queued reveal job and run its body as the worker role."""
    worker = create_async_engine(pg.url("worker"))
    try:
        for _ in range(200):
            rows = await pg.run(
                "super",
                "SELECT id, args FROM assess_jobs WHERE kind = 'config_reveal' "
                "AND state = 'ready' ORDER BY id DESC LIMIT 1",
            )
            if rows:
                job_id, args = rows[0]
                await pg.run(
                    "super",
                    "UPDATE assess_jobs SET state = 'done' WHERE id = :i",
                    {"i": job_id},
                )
                if seen is not None:
                    seen.append(dict(args))
                await reveal_field(pair[1], _acquire(worker), args)
                return
            await asyncio.sleep(0.025)
        raise AssertionError("no config_reveal job was queued")
    finally:
        await worker.dispose()


async def reveal_rows(pg, site: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super",
        "SELECT count(*) FROM assess_config_reveals WHERE site_id = :s",
        {"s": site},
    )
    return int(rows[0][0])


async def reveal_audits(pg, site: uuid.UUID) -> list:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT reason_code, data, actor_kind FROM assess_audit_events WHERE site_id = :s "
        "AND event = 'assess_teacher_config_revealed' ORDER BY id",
        {"s": site},
    )


async def reveal_jobs(pg) -> int:  # type: ignore[no-untyped-def]
    rows = await pg.run(
        "super", "SELECT count(*) FROM assess_jobs WHERE kind = 'config_reveal'"
    )
    return int(rows[0][0])


@pytest.mark.parametrize(
    ("field", "expected"),
    [(SOLUTION, TEXT), (PASTED_RUBRIC, RUBRIC), (TRANSCRIPT_RECIPIENTS, [A, B])],
)
async def test_a_fresh_editor_reads_the_field_back_once_and_nothing_stays(
    pg, pair, field: str, expected: Any, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)
        seen: list[dict[str, Any]] = []
        r, _ = await asyncio.gather(
            w.client.post(url(70), json=body(field), headers=auth("teacher")),
            serve_one(pg, pair, seen),
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"field": field, "value": expected}
        assert r.headers["cache-control"] == "no-store"
        # the job carries ids and a public key only, never a value
        (args,) = seen
        assert set(args) == {"site_id", "cmid", "field", "request_id", "public_key"}
        assert args["field"] == field and args["cmid"] == 70
        assert "canary" not in json.dumps(args) and "uni.example" not in json.dumps(
            args
        )
        # the API deleted the row it read
        assert await reveal_rows(pg, w.site) == 0
        # one audit row: who, which activity, which field; never the content
        (audit,) = await reveal_audits(pg, w.site)
        assert audit.reason_code == field and audit.actor_kind == "api"
        assert audit.data == {"actor": EDITOR, "cmid": 70, "field": field}
        # nothing of any value reached a log line, API or worker side
        out = capsys.readouterr()
        logged = out.out + out.err
        assert "assess_config_reveal" in logged  # the lines were captured
        for secret in ("canary", "uni.example", "Soluzione", "Criterio"):
            assert secret not in logged


async def test_the_worker_row_is_ciphertext_under_the_ephemeral_key_only(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)
        eph = EphemeralKey.generate()
        rid = uuid.uuid4()
        worker = create_async_engine(pg.url("worker"))
        try:
            await reveal_field(
                pair[1],
                _acquire(worker),
                {
                    "site_id": str(w.site),
                    "cmid": 70,
                    "field": SOLUTION,
                    "request_id": str(rid),
                    "public_key": eph.public_hex,
                },
            )
        finally:
            await worker.dispose()
        (row,) = await pg.run(
            "super",
            "SELECT status, sealed_value FROM assess_config_reveals WHERE id = :i",
            {"i": rid},
        )
        assert row.status == "ok"
        blob = bytes(row.sealed_value)
        assert TEXT.encode() not in blob and b"canary" not in blob
        assert eph.open(blob, w.site, 70, SOLUTION, rid).decode() == TEXT
        # bound to its own site, cmid, field and request: any other binding does not open
        for other in (
            (uuid.uuid4(), 70, SOLUTION, rid),
            (w.site, 71, SOLUTION, rid),
            (w.site, 70, PASTED_RUBRIC, rid),
            (w.site, 70, SOLUTION, uuid.uuid4()),
        ):
            with pytest.raises(Exception):  # noqa: B017 - InvalidTag
                eph.open(blob, *other)
        # nor with another ephemeral key
        with pytest.raises(Exception):  # noqa: B017
            EphemeralKey.generate().open(blob, w.site, 70, SOLUTION, rid)


async def test_an_unset_field_is_404_and_queues_nothing(pg, pair) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        before = await reveal_jobs(pg)
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 404, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-030"
        assert e["details"] == {"reason": "field_not_set", "field": SOLUTION}
        assert await reveal_jobs(pg) == before
        # cleared after being set: still not set
        await seal_fields(pg, pair, w.site)
        async with pg.conn("api", site=w.site) as conn:
            await store_sealed_field(
                conn, pair[0], w.site, 70, SOLUTION, None, actor_userid=1
            )
            await conn.commit()
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-030"
        assert await reveal_audits(pg, w.site) == []


async def test_a_field_cleared_before_the_worker_reads_it_is_404(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """The worker re-reads the row: a field cleared meanwhile answers ``not_set``."""
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)

        async def clear_then_serve() -> None:
            for _ in range(200):
                if await reveal_jobs(pg):
                    break
                await asyncio.sleep(0.025)
            await pg.run(
                "super",
                "UPDATE assess_activity_config_sealed SET sealed_blob = NULL, kid = NULL "
                "WHERE site_id = :s AND field = 'solution'",
                {"s": w.site},
            )
            await serve_one(pg, pair)

        r, _ = await asyncio.gather(
            w.client.post(url(70), json=body(), headers=auth("teacher")),
            clear_then_serve(),
        )
        assert r.status_code == 404 and err(r)["code"] == "ERR-ASSESS-030"
        assert await reveal_rows(pg, w.site) == 0
        assert await reveal_audits(pg, w.site) == []


@pytest.mark.parametrize(
    "field", ["opening", "SOLUTION", "", None, 3, ["solution"], "rubric"]
)
async def test_an_unknown_field_is_a_400(pg, pair, field: Any) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        r = await w.client.post(url(70), json=body(field), headers=auth("teacher"))
        assert r.status_code == 400, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-014" and e["details"] == {"reason": "field"}
        doc = body()
        del doc["field"]
        r = await w.client.post(url(70), json=doc, headers=auth("teacher"))
        assert r.status_code == 400 and err(r)["details"] == {"reason": "field"}


async def test_no_answer_in_time_is_a_retryable_503_and_nothing_is_audited(
    pg, pair
) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair, timeout_s=0.5) as w:
        await seal_fields(pg, pair, w.site)
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 503, r.text
        e = err(r)
        assert e["code"] == "ERR-ASSESS-031"
        assert e["details"] == {"reason": "reveal_unavailable", "retry_after_s": 2}
        assert e["category"] == "TRANSIENT"
        assert r.headers["retry-after"] == "2"
        assert await reveal_audits(pg, w.site) == []
        # a late answer is never read by anyone: the sweeper removes it
        await serve_one(pg, pair)
        assert await reveal_rows(pg, w.site) == 1
        await pg.run(
            "super",
            "UPDATE assess_config_reveals SET created_at = now() - interval '10 minutes'",
        )
        worker = create_async_engine(pg.url("worker"))
        try:
            assert await sweep_config_reveals(_acquire(worker)) >= 1
        finally:
            await worker.dispose()
        assert await reveal_rows(pg, w.site) == 0


async def test_a_row_of_another_cmid_or_field_is_never_used(
    pg, pair, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The poll matches its own request id AND site, cmid and field: a planted row with the
    same id for another cmid (or field) is not read, the request times out."""
    rid = uuid.uuid4()
    monkeypatch.setattr(reveal_api, "uuid4", lambda: rid)
    async with ready(pg, pair, timeout_s=0.6) as w:
        await seal_fields(pg, pair, w.site)
        await pg.run(
            "super",
            "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status, "
            "created_at) VALUES (:i, :s, 71, 'solution', 'not_set', "
            "clock_timestamp() + interval '1 second')",
            {"i": rid, "s": w.site},
        )
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 503 and err(r)["code"] == "ERR-ASSESS-031"


async def test_an_unreadable_seal_is_503_and_the_row_is_still_deleted(pg, pair) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)
        wrong = config_pair()  # a worker with another private key cannot open the field
        r, _ = await asyncio.gather(
            w.client.post(url(70), json=body(), headers=auth("teacher")),
            serve_one(pg, wrong),
        )
        assert r.status_code == 503 and err(r)["code"] == "ERR-ASSESS-031"
        assert await reveal_rows(pg, w.site) == 0
        assert await reveal_audits(pg, w.site) == []


async def test_unverified_teachers_are_refused_like_the_put(pg, pair) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)
        await seal_fields(pg, pair, w.site, 71)
        before = await reveal_jobs(pg)
        # stale facts: 403 with the retry hint, a refresh queued, audited
        await seed_gate(pg, w.site, 71, age="10 minutes")
        r = await w.client.post(url(71), json=body(), headers=auth("teacher"))
        assert r.status_code == 403
        e = err(r)
        assert e["code"] == "ERR-ASSESS-021"
        assert e["details"]["reason"] == "editor_check_stale"
        assert "retry_after_s" in e["details"] and "retry-after" in r.headers
        # a user who is not an editor: final 403
        r = await w.client.post(
            url(70), json=body(actor_userid=OTHER), headers=auth("teacher")
        )
        assert r.status_code == 403 and err(r)["details"]["reason"] == "not_an_editor"
        rows = await pg.run(
            "super",
            "SELECT reason_code, data FROM assess_audit_events WHERE site_id = :s "
            "AND event = 'assess_teacher_unverified' ORDER BY id",
            {"s": w.site},
        )
        assert [x.reason_code for x in rows] == ["stale", "not_an_editor"]
        assert {x.data["route"] for x in rows} == {"reveal_config"}
        assert {x.data["role"] for x in rows} == {"editor"}
        # an admin key or a mint key never reaches the route (exact scope)
        for name in ("admin", "mint"):
            r = await w.client.post(url(70), json=body(), headers=auth(name))
            assert r.status_code == 403 and err(r)["code"] == "ERR-AUTH-003"
        # a teacher key of another site: that site has no facts for the cmid
        other = await make_key(pg, ["assess:teacher"], w.other_site, "teacher")
        w.app.state.registry.get("key_store", "default").table["other_teacher"] = other
        r = await w.client.post(url(70), json=body(), headers=auth("other_teacher"))
        assert r.status_code == 403
        assert await reveal_jobs(pg) == before  # nothing queued for any refusal
        assert await reveal_audits(pg, w.site) == []


async def test_the_api_role_reads_no_reveal_row_of_another_site(pg, pair) -> None:  # type: ignore[no-untyped-def]
    """RLS: bound to site B, the API role sees none of site A's rows; it may not insert any."""
    async with ready(pg, pair) as w:
        await pg.run(
            "super",
            "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status) "
            "VALUES (:i, :s, 70, 'solution', 'not_set')",
            {"i": uuid.uuid4(), "s": w.site},
        )
        rows = await pg.run(
            "api", "SELECT count(*) FROM assess_config_reveals", site=w.other_site
        )
        assert rows[0][0] == 0
        rows = await pg.run(
            "api", "SELECT count(*) FROM assess_config_reveals", site=w.site
        )
        assert rows[0][0] == 1
        with pytest.raises(DBAPIError) as exc:
            await pg.run(
                "api",
                "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status) "
                "VALUES (:i, :s, 70, 'solution', 'not_set')",
                {"i": uuid.uuid4(), "s": w.site},
                site=w.site,
            )
        assert exc.value.orig.sqlstate == "42501"


async def test_the_row_shape_is_enforced(pg, pair) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.exc import IntegrityError

    async with ready(pg, pair) as w:
        for status, value in (("ok", None), ("not_set", b"\x01"), ("leaked", None)):
            with pytest.raises(IntegrityError):
                await pg.run(
                    "super",
                    "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status, "
                    "sealed_value) VALUES (:i, :s, 70, 'solution', :st, :v)",
                    {"i": uuid.uuid4(), "s": w.site, "st": status, "v": value},
                )
        with pytest.raises(IntegrityError):
            await pg.run(
                "super",
                "INSERT INTO assess_config_reveals (id, site_id, cmid, field, status) "
                "VALUES (:i, :s, 70, 'opening', 'not_set')",
                {"i": uuid.uuid4(), "s": w.site},
            )


async def test_a_malformed_job_writes_nothing(pg, pair) -> None:  # type: ignore[no-untyped-def]
    async with ready(pg, pair) as w:
        await seal_fields(pg, pair, w.site)
        eph = EphemeralKey.generate()
        good = {
            "site_id": str(w.site),
            "cmid": 70,
            "field": SOLUTION,
            "request_id": str(uuid.uuid4()),
            "public_key": eph.public_hex,
        }
        worker = create_async_engine(pg.url("worker"))
        try:
            for bad in (
                {"field": "opening"},
                {"public_key": "00" * 31},
                {"public_key": "zz" * 32},
                {"request_id": "nope"},
                {"site_id": "nope"},
                {"cmid": "70"},
            ):
                await reveal_field(pair[1], _acquire(worker), {**good, **bad})
        finally:
            await worker.dispose()
        assert await reveal_rows(pg, w.site) == 0


async def test_the_outcome_is_counted(
    pg, pair, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    monkeypatch.setattr(reveal_api, "count_outcome", seen.append)
    async with ready(pg, pair, timeout_s=0.5) as w:
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 404
        await seal_fields(pg, pair, w.site)
        r, _ = await asyncio.gather(
            w.client.post(url(70), json=body(), headers=auth("teacher")),
            serve_one(pg, pair),
        )
        assert r.status_code == 200
        r = await w.client.post(url(70), json=body(), headers=auth("teacher"))
        assert r.status_code == 503
        r = await w.client.post(
            url(70), json=body(actor_userid=OTHER), headers=auth("teacher")
        )
        assert r.status_code == 403
    assert seen == ["not_set", "revealed", "timeout", "refused"]
