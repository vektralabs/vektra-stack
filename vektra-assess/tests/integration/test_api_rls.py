"""Row-level security for the API role (ADR-0033 D9.2, plan 3.2), counted on a real cluster.

The static backstop (``test_site_predicate_static.py``) proves that every API statement names
the site; it cannot prove what the database does when one does not. This file does:

1. the catalog: every site-scoped table the API role can read has RLS enabled (not forced), the
   API policy binds ``app.assess_site_id``, the worker and the definer keep every row;
2. the backstop itself: an API connection bound to site A, running a route's statement with its
   ``site_id`` predicate removed, reads 0 rows of site B, and still reads its own;
3. fail closed: no binding, an empty one, or a binding left from an earlier transaction of the
   same pooled connection read 0 rows; a malformed one is an error, never a wider read;
4. writes: a bound API connection cannot insert or move a row into another site;
5. the cross-site paths that stay legitimate run through SECURITY DEFINER functions;
6. the policy predicate is index-friendly;
7. through the routes: a planted route that resolves its site from its key and reads without
   the predicate sees its own site only.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from support.world import auth, configure_activity, make_key, make_site, world

from vektra_assess.api import start as start_route
from vektra_assess.api.common import engine_of, site_of_key
from vektra_assess.api.site_binding import (
    bind_site,
    install_site_binding,
    reset_site,
    reset_site_dependency,
    set_request_site,
)
from vektra_assess.db import grants as g
from vektra_shared.auth import ApiKeyInfo, require_exact_scope

from .conftest import Pg, make_session

pytestmark = pytest.mark.integration

#: the tables the policy must cover, read from the catalog (a new table is covered or this
#: fails): a ``site_id`` column and SELECT for the API role (the audit table is insert-only for
#: the API and stays out, see the revision's docstring)
COVERED = (
    "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
    "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
    "AND c.relname LIKE 'assess\\_%' AND a.attname = 'site_id' AND NOT a.attisdropped "
    # any column: two of them are granted column by column
    "AND has_any_column_privilege('vektra_assess_api', c.oid, 'SELECT') ORDER BY 1"
)
BIND = "SELECT set_config('app.assess_site_id', :s, true)"


async def _two_sites(pg: Pg) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Sites A and B, each with a session, an activity, a minted jti and a Start attempt."""
    a, b = await make_site(pg), await make_site(pg)
    sids = []
    for site in (a, b):
        sid = await make_session(pg)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET site_id = :s, cmid = 77 WHERE id = :i",
            {"s": site, "i": sid},
        )
        sids.append(sid)
        await configure_activity(pg, site, 77)
        await pg.run(
            "super",
            "INSERT INTO assess_token_jti (jti, site_id, student_userid, cmid, mint_key_id, "
            "expires_at) VALUES (:j, :s, 1, 77, :k, now() + interval '1 hour')",
            {"j": f"jti-{uuid.uuid4().hex}", "s": site, "k": uuid.uuid4()},
        )
        await pg.run(
            "super",
            "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
            "VALUES (:s, 77, 1, 'requested')",
            {"s": site},
        )
    return a, b, sids[0], sids[1]


async def _as_api(
    pg: Pg, site: uuid.UUID | str | None, sql: str, **params: Any
) -> list[Any]:
    async with pg.conn("api") as c:
        if site is not None:
            await c.execute(text(BIND), {"s": str(site)})
        rows = list((await c.execute(text(sql), params)).fetchall())
        await c.rollback()
        return rows


# --- 1. the catalog ----------------------------------------------------------------------


async def test_every_site_scoped_table_the_api_reads_has_the_policy(pg: Pg) -> None:
    async with pg.conn("super") as c:
        covered = [r[0] for r in (await c.execute(text(COVERED))).fetchall()]
        flags = {
            r[0]: (r[1], r[2])
            for r in (
                await c.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(:t)"
                    ),
                    {"t": covered},
                )
            ).fetchall()
        }
        policies = (
            await c.execute(
                text(
                    "SELECT tablename, policyname, permissive, roles::text[], cmd, qual, "
                    "with_check FROM pg_policies WHERE schemaname = 'public' "
                    "AND tablename LIKE 'assess\\_%' ORDER BY 1, 2"
                )
            )
        ).fetchall()
    # false-pass guard: the query sees the tables, among them the ones the routes live on
    assert {"assess_sessions", "assess_site_keys", "assess_activity_config"} <= set(
        covered
    )
    assert len(covered) == 20, covered
    # enabled, not forced: the owner (the migration user) keeps every row, so a later data
    # migration and the base revision's own downgrade check still see what is there
    assert flags == {t: (True, False) for t in covered}
    by_table: dict[str, dict[str, Any]] = {}
    for p in policies:
        by_table.setdefault(p.tablename, {})[p.policyname] = p
    assert set(by_table) == set(covered), "a policy on a table outside the covered set"
    for table in covered:
        pols = by_table[table]
        assert set(pols) == {"assess_api_site_bound", "assess_unbound_roles"}, table
        api = pols["assess_api_site_bound"]
        assert (api.permissive, api.roles, api.cmd) == ("PERMISSIVE", [g.API], "ALL")
        for expr in (api.qual, api.with_check):
            assert "current_setting('app.assess_site_id'::text, true)" in expr, expr
            assert "NULLIF" in expr and "site_id =" in expr, expr
        trusted = pols["assess_unbound_roles"]
        assert sorted(trusted.roles) == sorted([g.WORKER, g.DEFINER])
        assert (trusted.qual, trusted.with_check) == ("true", "true")


# --- 2. the backstop ---------------------------------------------------------------------


async def test_a_route_statement_without_its_site_predicate_reads_nothing_of_site_b(
    pg: Pg,
) -> None:
    """The plan's acceptance test: the Start route's activity read (``start._ACTIVITY``) with
    its ``site_id`` predicate removed, run as the API role bound to site A, for site B's
    activity. The statement is otherwise valid: bound to B it finds the row."""
    a, b, _sa, _sb = await _two_sites(pg)
    sql = str(start_route._ACTIVITY)
    assert "site_id = :s AND " in sql, (
        "the route's statement changed: re-plant the removal"
    )
    leaky = sql.replace("site_id = :s AND ", "")
    assert "site_id" not in leaky.split("WHERE", 1)[1]
    assert await _as_api(pg, a, leaky, c=77) != []  # its own row
    seen_from_a = await _as_api(
        pg, a, "SELECT site_id FROM assess_activity_config WHERE cmid = 77"
    )
    assert [r[0] for r in seen_from_a] == [a]
    seen_from_b = await _as_api(
        pg, b, "SELECT site_id FROM assess_activity_config WHERE cmid = 77"
    )
    assert [r[0] for r in seen_from_b] == [b]
    # the same predicate-less reads on the other tables seeded for both sites
    for table in ("assess_sessions", "assess_token_jti", "assess_start_attempts"):
        rows = await _as_api(pg, a, f"SELECT DISTINCT site_id FROM {table}")
        assert [r[0] for r in rows] == [a], table


async def test_a_session_read_by_id_alone_cannot_reach_another_site(pg: Pg) -> None:
    """The allowlisted by-id reads of the static test (``session_gate``'s row read): bound to
    A, site B's session id reads no row, whatever the caller compares afterwards."""
    a, b, sid_a, sid_b = await _two_sites(pg)
    row_sql = "SELECT id, site_id FROM assess_sessions WHERE id = :i"
    assert await _as_api(pg, a, row_sql, i=sid_b) == []
    assert [tuple(r) for r in await _as_api(pg, a, row_sql, i=sid_a)] == [(sid_a, a)]
    assert [tuple(r) for r in await _as_api(pg, b, row_sql, i=sid_b)] == [(sid_b, b)]


# --- 3. fail closed ----------------------------------------------------------------------


@pytest.mark.parametrize("binding", [None, ""])
async def test_without_a_binding_the_api_role_reads_nothing(
    pg: Pg, binding: str | None
) -> None:
    await _two_sites(pg)
    async with pg.conn("super") as c:
        covered = [r[0] for r in (await c.execute(text(COVERED))).fetchall()]
    for table in covered:
        assert await _as_api(pg, binding, f"SELECT 1 FROM {table}") == [], table


async def test_a_binding_ends_with_its_transaction_on_a_pooled_connection(
    pg: Pg,
) -> None:
    """``set_config(..., true)`` is transaction-local: a pooled connection handed to the next
    request after a commit carries an empty value, which reads nothing (never the last site)."""
    a, _b, _sa, _sb = await _two_sites(pg)
    engine = create_async_engine(pg.url("api"), pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as c:
            await c.execute(text(BIND), {"s": str(a)})
            assert (
                await c.execute(text("SELECT count(*) FROM assess_sessions"))
            ).scalar() == 1
        async with engine.begin() as c:  # the same physical connection
            left = (
                await c.execute(
                    text("SELECT current_setting('app.assess_site_id', true)")
                )
            ).scalar()
            assert left in ("", None)
            assert (
                await c.execute(text("SELECT count(*) FROM assess_sessions"))
            ).scalar() == 0
    finally:
        await engine.dispose()


async def test_a_malformed_binding_is_an_error_never_a_wider_read(pg: Pg) -> None:
    await _two_sites(pg)
    with pytest.raises(DBAPIError) as exc:
        await _as_api(pg, "not-a-uuid", "SELECT 1 FROM assess_sessions")
    assert exc.value.orig.sqlstate == "22P02"  # invalid_text_representation


# --- 4. writes ---------------------------------------------------------------------------


async def test_a_bound_api_connection_cannot_write_into_another_site(pg: Pg) -> None:
    a, b, _sa, _sb = await _two_sites(pg)
    insert = (
        "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
        "VALUES (:s, 78, 1, 'requested')"
    )
    async with pg.conn("api") as c:
        await c.execute(text(BIND), {"s": str(a)})
        await c.execute(text(insert), {"s": a})  # its own site: accepted
        await c.rollback()
    with pytest.raises(DBAPIError) as exc:
        async with pg.conn("api") as c:
            await c.execute(text(BIND), {"s": str(a)})
            await c.execute(text(insert), {"s": b})
    assert exc.value.orig.sqlstate == "42501"
    assert "row-level security" in str(exc.value.orig)
    # unbound, even its own site's row is refused (fail closed on writes too)
    with pytest.raises(DBAPIError):
        await pg.run("api", insert, {"s": a})


# --- 5. the other roles and the definer paths ----------------------------------------------


async def test_the_worker_still_sees_and_writes_every_site(pg: Pg) -> None:
    a, b, _sa, _sb = await _two_sites(pg)
    rows = await pg.run("worker", "SELECT DISTINCT site_id FROM assess_sessions")
    assert {a, b} <= {r[0] for r in rows}
    # a stray binding changes nothing for the worker: its policy is unconditional
    async with pg.conn("worker") as c:
        await c.execute(text(BIND), {"s": str(a)})
        seen = {
            r[0]
            for r in (
                await c.execute(text("SELECT site_id FROM assess_sessions"))
            ).fetchall()
        }
        await c.rollback()
    assert {a, b} <= seen
    await pg.run(
        "worker",
        "INSERT INTO assess_start_attempts (site_id, cmid, student_userid, outcome) "
        "VALUES (:s, 79, 1, 'requested')",
        {"s": b},
    )


async def test_the_definer_functions_the_api_calls_still_work(pg: Pg) -> None:
    """A SECURITY DEFINER function runs as ``vektra_assess_definer``, which owns no table: it
    is under RLS like any role, and keeps every row only through its own policy. Each function
    the API calls, run as the API role, against rows of two sites."""
    a, b, sid_a, sid_b = await _two_sites(pg)
    # the cross-site counts and lookups that replace the API's direct reads: the API role,
    # bound to one site, gets exactly the deployment-wide count the superuser sees (the module
    # container holds sessions of earlier tests too, so the reference is counted, not assumed)
    every = "SELECT count(*) FROM assess_sessions WHERE state <> 'CLOSED'"
    expected = (await pg.run("super", every))[0][0]
    assert expected >= 2
    live = await pg.run("api", "SELECT assess_live_session_count()", site=a)
    assert live[0][0] == expected
    # its own read of the same table, bound to a, sees only a's live sessions
    own = await pg.run("api", every, site=a)
    assert own[0][0] == 1 < expected
    key = await make_key(pg, ["assess:teacher"], b, "teacher")
    got = await pg.run(
        "api",
        "SELECT id, disabled FROM assess_site_of_key(:k, 'teacher')",
        {"k": key.key_id},
    )
    assert [tuple(r) for r in got] == [(b, False)]
    assert (
        await pg.run(
            "api", "SELECT * FROM assess_site_of_key(:k, 'mint')", {"k": key.key_id}
        )
        == []
    )
    # the session functions, bound to the session's site
    async with pg.conn("api") as c:
        await c.execute(text(BIND), {"s": str(a)})
        material = (
            await c.execute(
                text("SELECT seq FROM get_session_material(:s, 1)"), {"s": sid_a}
            )
        ).fetchall()
        assert len(material) == 1
        closed = (
            await c.execute(
                text("SELECT assess_close_session(:s, 1, 'finished')"), {"s": sid_a}
            )
        ).scalar()
        assert closed is True
        await c.rollback()
    # the worker's close of site B's session, unbound, as before
    closed_b = await pg.run(
        "worker", "SELECT assess_close_session(:s, 1, 'finished')", {"s": sid_b}
    )
    assert closed_b[0][0] is True


async def test_the_pending_reply_scan_spans_sites_and_names_each_site(pg: Pg) -> None:
    a, b, sid_a, sid_b = await _two_sites(pg)
    await pg.run(
        "super",
        "UPDATE assess_sessions SET pending_reply_since = now() - interval '1 minute', "
        "deadline_at = now() + interval '10 minutes' WHERE id = ANY(:i)",
        {"i": [sid_a, sid_b]},
    )
    rows = await pg.run("api", "SELECT id, site_id FROM assess_pending_replies(0, 50)")
    assert {(sid_a, a), (sid_b, b)} <= {tuple(r) for r in rows}


# --- 6. performance ----------------------------------------------------------------------


async def test_the_policy_predicate_is_an_index_condition(pg: Pg) -> None:
    a, _b, _sa, _sb = await _two_sites(pg)
    async with pg.conn("api") as c:
        await c.execute(text(BIND), {"s": str(a)})
        await c.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in (
                await c.execute(
                    text("EXPLAIN SELECT id FROM assess_sessions WHERE cmid = 77")
                )
            ).fetchall()
        )
        await c.rollback()
    assert "Index" in plan and "site_id = (NULLIF" in plan, plan


# --- 7. through the routes ----------------------------------------------------------------

_guard = require_exact_scope("assess:teacher")
#: mounted like the production router: its first dependency resets the binding
planted = APIRouter(dependencies=[Depends(reset_site_dependency)])


@planted.get("/api/v1/assess/planted/sessions")
async def planted_sessions(
    request: Request, key: ApiKeyInfo = Depends(_guard)
) -> dict[str, Any]:
    """A route that forgot its ``site_id`` predicate (the regression RLS exists for)."""
    async with engine_of(request).begin() as conn:
        site = await site_of_key(conn, key, "teacher")
        rows = (
            await conn.execute(text("SELECT site_id FROM assess_sessions"))
        ).fetchall()
    # and a second transaction of the same request: bound at its start, without a call
    async with engine_of(request).connect() as conn:
        later = (
            await conn.execute(text("SELECT site_id FROM assess_sessions"))
        ).fetchall()
    return {
        "site": str(site),
        "sites": sorted({str(r[0]) for r in rows}),
        "later": sorted({str(r[0]) for r in later}),
    }


@planted.get("/api/v1/assess/planted/unbound")
async def planted_unbound(
    request: Request, key: ApiKeyInfo = Depends(_guard)
) -> dict[str, Any]:
    """The same read before any site is known: nothing at all."""
    async with engine_of(request).connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM assess_sessions"))).scalar()
    return {"n": n}


async def test_through_a_route_a_missing_predicate_reads_the_key_site_only(
    pg: Pg,
) -> None:
    async with world(pg) as w:
        w.app.include_router(planted)
        for site in (w.site, w.other_site):
            sid = await make_session(pg)
            await pg.run(
                "super",
                "UPDATE assess_sessions SET site_id = :s WHERE id = :i",
                {"s": site, "i": sid},
            )
        r = await w.client.get(
            "/api/v1/assess/planted/sessions", headers=auth("teacher")
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["site"] == str(w.site)
        assert body["sites"] == [str(w.site)]
        assert body["later"] == [str(w.site)]
        # the next request starts unbound: no site carried over from the previous one
        guard = w.extra["binding_guard"]
        assert guard.take() == []  # the bound route above ran clean
        r = await w.client.get(
            "/api/v1/assess/planted/unbound", headers=auth("teacher")
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"n": 0}
        # positive control of the runtime guard every route test runs under: it saw the read
        assert guard.take() == ["SELECT count(*) FROM assess_sessions"]
        # positive control of the control: the superuser sees both sites
        both = await pg.run(
            "super",
            "SELECT count(DISTINCT site_id) FROM assess_sessions WHERE site_id = ANY(:s)",
            {"s": [w.site, w.other_site]},
        )
        assert both[0][0] == 2


async def test_binding_a_second_site_in_one_request_is_refused(pg: Pg) -> None:
    """One request, one site: rebinding to another site is a programming error, never a
    silent switch."""
    a, b = await make_site(pg), await make_site(pg)
    engine = create_async_engine(pg.url("api"))
    try:
        async with engine.connect() as conn:
            await bind_site(conn, a)
            await bind_site(conn, a)  # the same site again is fine
            with pytest.raises(RuntimeError, match="already bound"):
                await bind_site(conn, b)
            await conn.rollback()
    finally:
        reset_site()
        await engine.dispose()


# --- review of #209 ------------------------------------------------------------------------


def _session_token(w: Any, *, site: uuid.UUID, sid: uuid.UUID) -> str:
    from vektra_assess.tokens import AssessTokens

    tokens = AssessTokens.from_settings(w.signing, w.settings)
    return tokens.mint_session(
        sub=1, site=site, cmid=1, sid=sid, seq=1, epoch=1, deadline=None
    ).token


async def _audit(pg: Pg, sid: uuid.UUID) -> list[tuple[str, str]]:
    rows = await pg.run(
        "super",
        "SELECT event, reason_code FROM assess_audit_events WHERE session_id = :s",
        {"s": sid},
    )
    return [tuple(r) for r in rows]


async def test_a_cross_site_session_probe_is_audited_as_an_ownership_mismatch(
    pg: Pg,
) -> None:
    """Review finding 1: before RLS, a site A token naming site B's session read the row and
    was audited ``not_owner`` (OWNERSHIP_MISMATCH_REJECTED). RLS hides the row; the gate asks
    the definer whether the id exists anywhere and keeps that classification. The HTTP answer
    is the not-found answer either way (no oracle)."""
    async with world(pg) as w:
        foreign = await make_session(pg)
        await pg.run(
            "super",
            "UPDATE assess_sessions SET site_id = :s WHERE id = :i",
            {"s": w.other_site, "i": foreign},
        )
        missing = uuid.uuid4()
        probe = await w.client.get(
            f"/api/v1/assess/sessions/{foreign}",
            headers=auth(_session_token(w, site=w.site, sid=foreign)),
        )
        nothing = await w.client.get(
            f"/api/v1/assess/sessions/{missing}",
            headers=auth(_session_token(w, site=w.site, sid=missing)),
        )
        assert probe.status_code == nothing.status_code == 404

        def outward(r: Any) -> dict[str, Any]:
            body = r.json()["error"]
            body.pop("request_id")  # unique per request
            return {
                "body": body,
                "headers": sorted(set(r.headers) - {"content-length"}),
            }

        assert outward(probe) == outward(nothing)
        assert await _audit(pg, foreign) == [
            ("assess_ownership_mismatch_rejected", "not_owner")
        ]
        assert await _audit(pg, missing) == [
            ("assess_in_session_gate_rejected", "not_found")
        ]


async def test_a_signed_token_with_a_malformed_site_is_the_audited_401(pg: Pg) -> None:
    """Review finding 4: a correctly signed session token whose ``site`` is not a uuid never
    reaches the binding as a 500: it is the token refusal, audited like any other."""
    import jwt

    async with world(pg) as w:
        sid = uuid.uuid4()
        good = _session_token(w, site=w.site, sid=sid)
        claims = jwt.decode(good, options={"verify_signature": False})
        claims["site"] = "not-a-uuid"
        bad = jwt.encode(
            claims,
            w.signing.active_key,
            algorithm="HS256",
            headers={"kid": w.signing.active},
        )
        r = await w.client.get(f"/api/v1/assess/sessions/{sid}", headers=auth(bad))
        assert r.status_code == 401, r.text
        assert await _audit(pg, sid) == [("assess_in_session_gate_rejected", "token")]


async def test_the_existence_check_answers_one_boolean(pg: Pg) -> None:
    # unbound, from no site: an id of site B exists, a random one does not; nothing else
    *_, sid_b = await _two_sites(pg)
    got = await pg.run("api", "SELECT assess_session_exists(:s)", {"s": sid_b})
    assert got == [(True,)]
    got = await pg.run("api", "SELECT assess_session_exists(:s)", {"s": uuid.uuid4()})
    assert got == [(False,)]


async def test_a_request_bound_to_a_learns_the_documented_minimum_of_the_definers(
    pg: Pg,
) -> None:
    """Review finding 8: the two lookups that cross the binding answer one value each, and
    nothing for an id they do not know."""
    a, b, _sa, sid_b = await _two_sites(pg)
    key_b = await make_key(pg, ["assess:teacher"], b, "teacher")
    async with pg.conn("api", site=a) as c:
        # the key -> site lookup: exactly (site, plugin minimum, disabled) for a known key id,
        # nothing for any other id
        res = await c.execute(
            text("SELECT * FROM assess_site_of_key(:k, 'teacher')"), {"k": key_b.key_id}
        )
        assert list(res.keys()) == ["id", "min_plugin_version", "disabled"]
        assert [r.id for r in res.fetchall()] == [b]
        unknown = await c.execute(
            text("SELECT * FROM assess_site_of_key(:k, 'teacher')"), {"k": uuid.uuid4()}
        )
        assert unknown.fetchall() == []
        # the transcript's site: one uuid or NULL, never a row of the transcript
        tid = uuid.uuid4()
        await pg.run(
            "super",
            "INSERT INTO assess_transcripts (id, session_id, transcript_kid, content_hash, "
            "shredded_at) VALUES (:t, :s, 'k', 'h', now())",
            {"t": tid, "s": sid_b},
        )
        known = await c.execute(text("SELECT assess_transcript_site(:t)"), {"t": tid})
        assert list(known.keys()) == ["assess_transcript_site"]
        assert known.scalar() == b
        nope = await c.execute(
            text("SELECT assess_transcript_site(:t)"), {"t": uuid.uuid4()}
        )
        assert nope.scalar() is None
        await c.rollback()


async def test_the_gate_binds_once_per_transaction(pg: Pg) -> None:
    """Review finding 5: a transaction already bound at its start (the request's site) is not
    bound again by ``bind_site``; one bound to another site is refused."""
    a, b = await make_site(pg), await make_site(pg)
    engine = install_site_binding(create_async_engine(pg.url("api")))
    sent: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if "set_config" in statement:
            sent.append(statement)

    from sqlalchemy import event

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        set_request_site(a)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))  # begins, bound by the listener
            await bind_site(conn, a)  # already bound to a: no second round trip
            assert len(sent) == 1
            await conn.rollback()
        reset_site()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))  # unbound transaction
            await bind_site(conn, b)
            assert len(sent) == 2
            reset_site()
            with pytest.raises(RuntimeError, match="already bound"):
                await bind_site(conn, a)  # the transaction is b's
            await conn.rollback()
    finally:
        reset_site()
        await engine.dispose()


async def test_pg_dump_by_a_role_rls_restricts_fails_loudly(pg: Pg) -> None:
    """Review finding 2, the operator doc's claim: pg_dump as a role that neither owns the
    tables nor has BYPASSRLS stops with an error (it never writes a silently partial dump); the
    superuser dumps the same table."""
    import asyncio

    from .conftest import ROLE_PASSWORD

    async def dump(user: str, password: str) -> tuple[int, str]:
        """``docker exec pg_dump`` without blocking the loop, bounded: killed after 60 s."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-e", f"PGPASSWORD={password}", pg.container_id,
            "pg_dump", "-h", "127.0.0.1", "-U", user, "--data-only",
            "-t", "assess_sessions", "test",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), 60)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        assert proc.returncode is not None
        return proc.returncode, err.decode()

    code, stderr = await dump("vektra_assess_api", ROLE_PASSWORD)
    assert code != 0
    assert "query would be affected by row-level security policy" in stderr
    assert (await dump("test", "test"))[0] == 0


async def test_an_invalidated_transaction_keeps_its_own_error_and_leaves_no_stale_site(
    pg: Pg,
) -> None:
    """CI e2e on #209: a transaction whose connection was invalidated (a disconnect inside
    the block) rolls back, and the rollback listener touched ``conn.info``, which on an
    invalidated connection raises ``PendingRollbackError`` and masked the original error. The
    listener must never raise; the next transaction on the engine works and is bound to the
    request's site only (no marker left from the dead connection)."""
    from sqlalchemy.exc import DBAPIError, PendingRollbackError

    a, b, sid_a, _sid_b = await _two_sites(pg)
    engine = install_site_binding(
        create_async_engine(pg.url("api"), pool_size=1, max_overflow=0)
    )
    try:
        # 1. invalidated by hand, then the block raises its own error
        set_request_site(a)
        with pytest.raises(LookupError, match="the original"):
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
                await conn.invalidate()
                raise LookupError("the original")
        # 2. a real disconnect: the session's own backend terminated mid-transaction
        with pytest.raises(DBAPIError) as exc:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT pg_terminate_backend(pg_backend_pid())")
                )
        assert not isinstance(exc.value, PendingRollbackError)
        # the engine still serves, bound to the request's site
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT id FROM assess_sessions"))
            ).fetchall()
            assert [r[0] for r in rows] == [sid_a]
            await conn.rollback()
        # a new request (no site yet) is unbound: no stale marker made bind_site skip
        reset_site()
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT count(*) FROM assess_sessions"))
            ).scalar() == 0
            await bind_site(conn, b)
            seen = (
                await conn.execute(text("SELECT DISTINCT site_id FROM assess_sessions"))
            ).fetchall()
            assert [r[0] for r in seen] == [b]
            await conn.rollback()
    finally:
        reset_site()
        await engine.dispose()
