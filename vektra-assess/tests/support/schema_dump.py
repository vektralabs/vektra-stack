"""The schema and privilege dumps behind the squash-equivalence test (plan 2a-3.1, 2a-3.4).

Two normalised texts describe a migrated database:

- :func:`schema_text`: ``pg_dump --schema-only`` of the whole database, without the noise that
  varies between two runs of the same schema (comments, ``SET`` lines, the ``\\restrict`` key of
  pg_dump 16.10+, the migration user's name), followed by the rows the migrations seed;
- :func:`privilege_text`: the attributes and memberships of the three assess roles and their
  EFFECTIVE privileges (``has_*_privilege``, so PUBLIC and memberships count) on every table,
  column, sequence and function of schema public.

The goldens under ``vektra-assess/tests/golden/`` were taken from the revisions 0009 to 0044 that
the assess base revision replaced, with :data:`RENAMES` applied (ADR-0033 D5). Regenerate them
after a new assess revision with ``VEKTRA_REGEN_SCHEMA_GOLDEN=1`` (see
``integration/test_schema_branch.py``) and review the diff: it must show that revision's change
and nothing else.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

#: the renames of ADR-0033 D5 that the squash applies to the old chain's head, longest first so
#: that no rename rewrites part of another. Every other difference is a failure.
RENAMES: tuple[tuple[str, str], ...] = (
    ("ck_assess_sites_moodle_origin", "ck_assess_sites_lms_origin"),
    ("moodle_site_uuid", "lms_site_uuid"),
    ("moodle_origin", "lms_origin"),
    ("moodle_release", "lms_release"),
)

ROLES = ("vektra_assess_api", "vektra_assess_worker", "vektra_assess_definer")

_DROP_LINE = re.compile(
    r"^(--.*|SET .*|SELECT pg_catalog\.set_config\(.*|\\(un)?restrict .*)$"
)


def apply_renames(sql: str) -> str:
    for old, new in RENAMES:
        sql = sql.replace(old, new)
    return sql


def normalise_dump(raw: str, migration_user: str) -> str:
    """The dump without comments, settings and blank runs; the migration user as ``<owner>``."""
    owner = re.compile(rf"\b{re.escape(migration_user)}\b")
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if _DROP_LINE.match(line):
            continue
        if not line and (not lines or not lines[-1]):
            continue
        lines.append(owner.sub("<owner>", line))
    return "\n".join(lines).strip() + "\n"


def pg_dump(container: Any, user: str, database: str) -> str:
    """``pg_dump --schema-only`` run inside the testcontainers Postgres (the same pg16 binary on
    every machine, none needed on the host)."""
    res = container.get_wrapped_container().exec_run(
        ["pg_dump", "-U", user, "--schema-only", database]
    )
    out = res.output.decode()
    if res.exit_code != 0:
        raise RuntimeError(f"pg_dump failed: {out}")
    return out


SEED_QUERY = (
    "SELECT 'namespaces', id || ' ' || display_name || ' ' || config::text "
    "FROM namespaces ORDER BY id"
)


async def schema_text(container: Any, conn: Any, user: str, database: str) -> str:
    """The normalised schema dump, then the seeded rows."""
    body = normalise_dump(pg_dump(container, user, database), user)
    seeds = (await conn.execute(text(SEED_QUERY))).fetchall()
    tail = "".join(f"-- seed {table}: {row}\n" for table, row in seeds)
    return body + "\n" + tail


_ROLE_QUERY = """
SELECT r.rolname, r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
       r.rolbypassrls,
       coalesce((SELECT string_agg(g.rolname, ',' ORDER BY g.rolname)
                 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                 WHERE m.member = r.oid), '')
FROM pg_roles r WHERE r.rolname = ANY(:roles) ORDER BY r.rolname
"""

_TABLE_QUERY = """
SELECT r.role, c.relname, p.priv
FROM (SELECT rolname AS role FROM pg_roles WHERE rolname = ANY(CAST(:roles AS text[]))) AS r
CROSS JOIN pg_class c
CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES',
                        'TRIGGER']) AS p(priv)
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v')
  AND has_table_privilege(r.role, c.oid, p.priv)
ORDER BY 1, 2, 3
"""

_COLUMN_QUERY = """
SELECT r.role, c.relname, a.attname, p.priv
FROM (SELECT rolname AS role FROM pg_roles WHERE rolname = ANY(CAST(:roles AS text[]))) AS r
CROSS JOIN pg_class c
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) AS p(priv)
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v')
  AND has_column_privilege(r.role, c.oid, a.attnum, p.priv)
  AND NOT has_table_privilege(r.role, c.oid, p.priv)
ORDER BY 1, 2, 3, 4
"""

_SEQUENCE_QUERY = """
SELECT r.role, c.relname, p.priv
FROM (SELECT rolname AS role FROM pg_roles WHERE rolname = ANY(CAST(:roles AS text[]))) AS r
CROSS JOIN pg_class c
CROSS JOIN unnest(ARRAY['USAGE','SELECT','UPDATE']) AS p(priv)
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S'
  AND has_sequence_privilege(r.role, c.oid, p.priv)
ORDER BY 1, 2, 3
"""

_FUNCTION_QUERY = """
SELECT r.role, f.oid::regprocedure::text
FROM (SELECT rolname AS role FROM pg_roles WHERE rolname = ANY(CAST(:roles AS text[]))) AS r
CROSS JOIN pg_proc f
WHERE f.pronamespace = 'public'::regnamespace
  AND has_function_privilege(r.role, f.oid, 'EXECUTE')
ORDER BY 1, 2
"""

_SCHEMA_QUERY = """
SELECT r.role, p.priv
FROM (SELECT rolname AS role FROM pg_roles WHERE rolname = ANY(CAST(:roles AS text[]))) AS r
CROSS JOIN unnest(ARRAY['USAGE','CREATE']) AS p(priv)
WHERE has_schema_privilege(r.role, 'public', p.priv)
ORDER BY 1, 2
"""


async def privilege_text(conn: Any) -> str:
    """Role attributes and memberships, then every effective privilege, one per line.

    Column privileges are listed only where the role lacks the same privilege on the whole
    table (a table grant implies every column)."""
    roles = list(ROLES)
    out: list[str] = []
    for row in (await conn.execute(text(_ROLE_QUERY), {"roles": roles})).fetchall():
        name, *flags, member_of = row
        names = ("super", "inherit", "createrole", "createdb", "login", "bypassrls")
        attrs = " ".join(f"{n}={'y' if v else 'n'}" for n, v in zip(names, flags))
        out.append(f"role {name} {attrs} member_of=[{member_of}]")
    for label, query in (
        ("schema", _SCHEMA_QUERY),
        ("table", _TABLE_QUERY),
        ("column", _COLUMN_QUERY),
        ("sequence", _SEQUENCE_QUERY),
        ("function", _FUNCTION_QUERY),
    ):
        for row in (await conn.execute(text(query), {"roles": roles})).fetchall():
            out.append(label + " " + " ".join(str(v) for v in row))
    return sort_lines("\n".join(out))


def sort_lines(listing: str) -> str:
    """One privilege per line, sorted: the order survives a rename (the golden of the old chain
    was sorted on the old names)."""
    return "\n".join(sorted(line for line in listing.splitlines() if line)) + "\n"
