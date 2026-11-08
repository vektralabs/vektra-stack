"""Revision 0036 (plan assess-teacher-ops 11a): the sealed teacher config, per field.

- the API role writes a sealed field and can read which fields are set, never the blob, the kid
  or the recipient refs (permission denied, SQLSTATE 42501: ARCH-071, OQ-034);
- the database audits a teacher's write of a sealed field old/new from the row itself (set /
  replaced / cleared; recipient refs, never an address), and a key rotation is not audited;
- RAG ON is unrepresentable (ARCH-072); the revision round-trips to 0035 and back.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from support.recipients import config_pair
from support.world import configure_activity, make_site

from vektra_assess.crypto.teacher_config import (
    SOLUTION,
    TRANSCRIPT_RECIPIENTS,
    store_sealed_field,
)
from vektra_assess.delivery.recipients import recipient_ref

from .conftest import _alembic

pytestmark = pytest.mark.integration

SECRET = "SOLUZIONE-CANARY-7731"


async def _audits(pg, site: uuid.UUID) -> list:  # type: ignore[no-untyped-def]
    return await pg.run(
        "super",
        "SELECT event, reason_code, actor_kind, data FROM assess_audit_events "
        "WHERE site_id = :s AND event = 'assess_teacher_config_changed' ORDER BY id",
        {"s": site},
    )


async def _store(pg, pub, site, field, value, actor, **kw) -> None:  # type: ignore[no-untyped-def]
    async with pg.conn("api") as conn:
        await store_sealed_field(
            conn, pub, site, 40, field, value, actor_userid=actor, **kw
        )
        await conn.commit()


async def test_the_api_writes_a_sealed_field_and_cannot_read_it_back(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await configure_activity(pg, site, 40)
    pub, _priv = config_pair()
    await _store(pg, pub, site, SOLUTION, SECRET, 9)
    rows = await pg.run(
        "api",
        "SELECT field, is_set, item_count, updated_by FROM assess_activity_config_sealed "
        "WHERE site_id = :s AND cmid = 40",
        {"s": site},
    )
    assert [tuple(r) for r in rows] == [(SOLUTION, True, None, 9)]
    for sql in (
        "SELECT sealed_blob FROM assess_activity_config_sealed",
        "SELECT kid FROM assess_activity_config_sealed",
        "SELECT refs FROM assess_activity_config_sealed",
        "SELECT * FROM assess_activity_config_sealed",
        # reading the blob through a predicate is a read too
        "SELECT field FROM assess_activity_config_sealed WHERE sealed_blob IS NOT NULL",
        "UPDATE assess_activity_config_sealed SET item_count = NULL "
        "WHERE sealed_blob IS NOT NULL",
        "DELETE FROM assess_activity_config_sealed",
    ):
        with pytest.raises(DBAPIError) as exc:
            await pg.run("api", sql)
        assert getattr(exc.value.orig, "sqlstate", None) == "42501", sql
    # and the plaintext is nowhere in the row (sealed, not stored)
    raw = await pg.run(
        "super",
        "SELECT sealed_blob FROM assess_activity_config_sealed WHERE site_id = :s",
        {"s": site},
    )
    assert SECRET.encode() not in bytes(raw[0][0])


async def test_every_sealed_write_is_audited_old_new_without_content(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await configure_activity(pg, site, 40)
    pub, _priv = config_pair()
    a, b = "prof.rossi@unical.it", "m.bianchi@unical.it"
    ra, rb = recipient_ref(site, a), recipient_ref(site, b)

    await _store(pg, pub, site, SOLUTION, None, 9)  # clearing an unset field: no change
    await _store(pg, pub, site, SOLUTION, SECRET, 9)
    await _store(pg, pub, site, SOLUTION, SECRET + "-2", 10)
    await _store(pg, pub, site, SOLUTION, None, 11)
    one = json.dumps([a])
    await _store(pg, pub, site, TRANSCRIPT_RECIPIENTS, one, 9, refs=[ra], item_count=1)
    # the same list sent again: re-sealed, not a change
    await _store(pg, pub, site, TRANSCRIPT_RECIPIENTS, one, 9, refs=[ra], item_count=1)
    two = json.dumps([a, b])
    await _store(
        pg, pub, site, TRANSCRIPT_RECIPIENTS, two, 12, refs=[ra, rb], item_count=2
    )
    rows = await _audits(pg, site)
    got = [(r.reason_code, r.data["action"], r.data["actor"]) for r in rows]
    assert got == [
        (SOLUTION, "set", 9),
        (SOLUTION, "replaced", 10),
        (SOLUTION, "cleared", 11),
        (TRANSCRIPT_RECIPIENTS, "set", 9),
        (TRANSCRIPT_RECIPIENTS, "replaced", 12),
    ]
    assert {r.actor_kind for r in rows} == {"api"}
    last = rows[-1].data
    assert (last["old_count"], last["new_count"]) == (1, 2)
    assert last["old_1"] == ra and {last["new_1"], last["new_2"]} == {ra, rb}
    dumped = json.dumps([r.data for r in rows])
    for secret in (SECRET, a, b, "unical"):
        assert secret not in dumped  # 0 secret content: refs and codes only

    # a key rotation re-seals blob and kid (the worker's only UPDATE columns): no audit row.
    # (the rotation itself walks every site's rows; here its UPDATE is run on this site's only)
    await pg.run(
        "worker",
        "UPDATE assess_activity_config_sealed SET sealed_blob = sealed_blob, kid = kid "
        "WHERE site_id = :s AND sealed_blob IS NOT NULL",
        {"s": site},
    )
    assert len(await _audits(pg, site)) == len(rows)


async def test_rag_on_is_unrepresentable(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await configure_activity(pg, site, 41)
    with pytest.raises(IntegrityError, match="ck_assess_activity_config_rag_off"):
        await pg.run(
            "super",
            "UPDATE assess_activity_config SET rag_enabled = true WHERE site_id = :s",
            {"s": site},
        )


async def test_the_recipient_count_matches_the_refs(pg) -> None:  # type: ignore[no-untyped-def]
    site = await make_site(pg)
    await configure_activity(pg, site, 42)
    pub, _ = config_pair()
    async with pg.conn("api") as conn:
        with pytest.raises(
            IntegrityError, match="ck_assess_activity_config_sealed_refs"
        ):
            await store_sealed_field(
                conn, pub, site, 42, TRANSCRIPT_RECIPIENTS, "[]", actor_userid=1,
                refs=["a" * 64] * 4, item_count=4,
            )  # fmt: skip


def test_round_trip_to_0035_and_back(pg) -> None:  # type: ignore[no-untyped-def]
    down = _alembic(pg.super_url, "downgrade", "0035")
    assert down.returncode == 0, down.stderr
    up = _alembic(pg.super_url, "upgrade", "head")
    assert up.returncode == 0, up.stderr


async def test_the_upgrade_refuses_a_composite_config_it_cannot_split(pg) -> None:  # type: ignore[no-untyped-def]
    assert _alembic(pg.super_url, "downgrade", "0035").returncode == 0
    site = await make_site(pg)
    try:
        await pg.run(
            "super",
            "INSERT INTO assess_activity_config (site_id, cmid, activity_type, "
            "teacher_config_sealed, teacher_config_kid) VALUES (:s, 43, 'quiz', '\\x01', 'w1')",
            {"s": site},
        )
        up = _alembic(pg.super_url, "upgrade", "head")
        assert up.returncode != 0 and "composite sealed teacher config" in up.stderr
    finally:
        await pg.run(
            "super",
            "DELETE FROM assess_activity_config WHERE site_id = :s",
            {"s": site},
        )
        assert _alembic(pg.super_url, "upgrade", "head").returncode == 0
    async with pg.conn("super") as c:
        assert (
            await c.execute(text("SELECT count(*) FROM assess_activity_config_sealed"))
        ).scalar() is not None
