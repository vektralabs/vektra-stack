"""The grant matrix against pg_catalog, both ways (plan assess-data-crypto 3.2, 3.3, 3.4).

The matrix (``vektra_assess.db.grants``) is compared to EFFECTIVE privileges
(``has_table_privilege`` / ``has_column_privilege`` / ``has_sequence_privilege`` /
``has_function_privilege``), so a grant that arrives through PUBLIC or a role membership is
caught too. A green migration is not evidence: the behavioural tests then connect as the
real roles and observe SQLSTATE 42501.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from vektra_assess.db import grants as g

from .conftest import ROOT, make_session

pytestmark = pytest.mark.integration

TABLE_PRIVS = ("DELETE", "TRUNCATE", "TRIGGER")
COLUMN_PRIVS = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


async def _columns(conn) -> dict[str, list[str]]:
    rows = (
        await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
            )
        )
    ).fetchall()
    cols: dict[str, list[str]] = {}
    for t, c in rows:
        cols.setdefault(t, []).append(c)
    return cols


async def _bool(conn, sql: str, **p) -> bool:
    return bool((await conn.execute(text(sql), p)).scalar())


async def grant_drift(conn) -> list[str]:
    """Every difference between the matrix and the effective privileges."""
    drift: list[str] = []
    cols = await _columns(conn)
    db_tables = {t for t in cols if t.startswith("assess_")}
    if not db_tables:
        return ["no assess_% table in the database: the migrations did not run"]
    if db_tables != set(g.TABLES):
        drift.append(f"tables not in the matrix: {sorted(db_tables ^ set(g.TABLES))}")

    for role in g.ROLES:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname=:r"
                ),
                {"r": role},
            )
        ).one()
        if any(row):
            drift.append(f"{role} has a dangerous attribute: {tuple(row)}")
        if await _bool(
            conn, "SELECT has_schema_privilege(:r,'public','CREATE')", r=role
        ):
            drift.append(f"{role} can CREATE in schema public")
        for table, tcols in cols.items():
            granted = g.MATRIX[role].get(table, {})
            for priv in TABLE_PRIVS:
                has = await _bool(
                    conn,
                    "SELECT has_table_privilege(:r, :t, :p)",
                    r=role,
                    t=table,
                    p=priv,
                )
                want = priv in granted and table.startswith("assess_")
                if has != want:
                    drift.append(f"{role} {priv} on {table}: has={has} want={want}")
            for priv in COLUMN_PRIVS:
                has_cols = {
                    c
                    for c in tcols
                    if await _bool(
                        conn,
                        "SELECT has_column_privilege(:r, :t, :c, :p)",
                        r=role,
                        t=table,
                        c=c,
                        p=priv,
                    )
                }
                if table.startswith("assess_") and priv in granted:
                    want_cols = (
                        set(tcols) if granted[priv] is None else set(granted[priv])
                    )
                else:
                    want_cols = set()
                if has_cols != want_cols:
                    drift.append(
                        f"{role} {priv} on {table}: extra={sorted(has_cols - want_cols)} "
                        f"missing={sorted(want_cols - has_cols)}"
                    )
        for seq in g.SEQUENCES:
            for priv in ("USAGE", "SELECT", "UPDATE"):
                has = await _bool(
                    conn,
                    "SELECT has_sequence_privilege(:r, :s, :p)",
                    r=role,
                    s=seq,
                    p=priv,
                )
                want = priv == "USAGE" and seq in g.SEQUENCE_USAGE.get(role, ())
                if has != want:
                    drift.append(
                        f"{role} {priv} on sequence {seq}: has={has} want={want}"
                    )

    for sig, allowed in g.FUNCTIONS.items():
        name = sig.split("(")[0]
        proc = (
            await conn.execute(
                text(
                    "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), "
                    "(SELECT count(*) FROM aclexplode(coalesce(p.proacl, "
                    " acldefault('f', p.proowner))) a WHERE a.grantee = 0) "
                    "FROM pg_proc p WHERE p.oid = to_regprocedure(:s)"
                ),
                {"s": f"public.{sig}"},
            )
        ).one_or_none()
        if proc is None:
            drift.append(f"function {name} does not exist")
            continue
        secdef, config, owner, public_execs = proc
        if not secdef:
            drift.append(f"{name} is not SECURITY DEFINER")
        if "search_path=pg_catalog, pg_temp" not in (config or []):
            drift.append(f"{name} search_path is not pinned: {config}")
        if owner != g.DEFINER:
            drift.append(f"{name} owned by {owner}")
        if public_execs:
            drift.append(f"{name} is executable by PUBLIC")
        for role in g.ROLES:
            has = await _bool(
                conn,
                "SELECT has_function_privilege(:r, to_regprocedure(:s), 'EXECUTE')",
                r=role,
                s=f"public.{sig}",
            )
            if has != (role in allowed or role == g.DEFINER):  # the owner always can
                drift.append(f"{role} EXECUTE on {name}: has={has}")
    return drift


async def test_matrix_matches_the_catalog_both_ways(pg):
    async with pg.conn("super") as conn:
        assert await grant_drift(conn) == []


async def test_canary_an_extra_grant_is_detected(pg):
    """3.4: the comparison bites. An extra grant inside a rolled-back transaction fails it."""
    async with pg.conn("super") as conn:
        await conn.execute(text("GRANT SELECT ON assess_turns TO vektra_assess_api"))
        drift = await grant_drift(conn)
        await conn.rollback()
    assert any("assess_turns" in d and "extra" in d for d in drift), drift
    async with pg.conn("super") as conn:
        await conn.execute(
            text("REVOKE SELECT ON assess_jobs FROM vektra_assess_worker")
        )
        drift = await grant_drift(conn)
        await conn.rollback()
    assert any("assess_jobs" in d and "missing" in d for d in drift), drift
    async with pg.conn("super") as conn:
        await conn.execute(
            text(
                "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid, integer) "
                "TO PUBLIC"
            )
        )
        drift = await grant_drift(conn)
        await conn.rollback()
    assert any("PUBLIC" in d for d in drift), drift


def test_migration_0015_is_the_rendered_matrix():
    path = ROOT / "migrations" / "versions" / "0015_assess_grants.py"
    spec = importlib.util.spec_from_file_location("rev0015", Path(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert list(mod.GRANT_STATEMENTS) == g.render_statements()


# --- 3.3 behavioural cross-check ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM assess_turns",
        "SELECT 1 FROM assess_snapshots",
        "SELECT 1 FROM assess_session_keys",
        "SELECT 1 FROM assess_session_handoff",
        "SELECT teacher_config_sealed FROM assess_activity_config",
        "SELECT * FROM assess_activity_config",
        "SELECT 1 FROM api_keys",
        "CREATE TABLE public.assess_evil (x int)",
        "TRUNCATE assess_sessions",
        "DELETE FROM assess_sessions",
    ],
)
async def test_api_role_is_denied_with_sqlstate_42501(pg, sql):
    with pytest.raises(DBAPIError) as exc:
        await pg.run("api", sql)
    assert getattr(exc.value.orig, "sqlstate", None) == "42501", exc.value


async def test_api_role_can_use_what_it_is_granted(pg):
    await pg.run("api", "SELECT id, teacher_config_set FROM assess_activity_config")
    await pg.run("api", "SELECT count(*) FROM assess_sessions")
    sid = await make_session(pg)
    await pg.run(
        "api", "UPDATE assess_sessions SET epoch = epoch WHERE id = :s", {"s": sid}
    )
    with pytest.raises(DBAPIError) as exc:  # state is not in the API's column list
        await pg.run(
            "api", "UPDATE assess_sessions SET state='CLOSED' WHERE id=:s", {"s": sid}
        )
    assert exc.value.orig.sqlstate == "42501"


async def test_worker_closing_a_session_with_raw_sql_leaves_no_handoff_row(pg):
    """2.5 as the worker role: no explicit DELETE of the hand-off row."""
    sid = await make_session(pg)
    await pg.run(
        "worker",
        "UPDATE assess_sessions SET state='CLOSED', close_reason='timed_out' WHERE id=:s",
        {"s": sid},
    )
    count = await pg.run(
        "super",
        "SELECT count(*) FROM assess_session_handoff WHERE session_id=:s",
        {"s": sid},
    )
    assert count[0][0] == 0
    with pytest.raises(DBAPIError, match="cannot be reopened"):
        await pg.run(
            "worker",
            "UPDATE assess_sessions SET state='ACTIVE', close_reason=NULL WHERE id=:s",
            {"s": sid},
        )


async def test_worker_cannot_update_snapshots_or_turns(pg):
    sid = await make_session(pg)
    for sql in (
        "UPDATE assess_snapshots SET ciphertext='\\x09' WHERE session_id=:s",
        "UPDATE assess_turns SET ciphertext='\\x09' WHERE session_id=:s",
        "SELECT 1 FROM api_keys",
        "TRUNCATE assess_turns",
    ):
        with pytest.raises(DBAPIError) as exc:
            await pg.run("worker", sql, {"s": sid} if ":s" in sql else None)
        assert exc.value.orig.sqlstate == "42501", sql
