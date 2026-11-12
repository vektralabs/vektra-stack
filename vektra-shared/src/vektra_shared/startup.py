"""Startup validation utilities (ARCH-057).

Steps 2 and 3 of the startup validation sequence:
  2. Database connectivity check (asyncpg connection test)
  3. Database schema check (Alembic current head = deployed head)

Steps 1, 4-8 are implemented in each component's startup logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


class StartupValidationError(Exception):
    """Raised when a startup validation step fails.

    Carries the variable name / step description and a human-readable
    remediation hint to help operators fix misconfiguration quickly.
    """

    def __init__(self, step: str, detail: str, remediation: str) -> None:
        self.step = step
        self.detail = detail
        self.remediation = remediation
        super().__init__(f"Startup validation failed at step '{step}': {detail}")

    def to_plain_text(self) -> str:
        """Format as the structured plain-text error message per ARCH-057."""
        return (
            f"[STARTUP ERROR] Step: {self.step}\n"
            f"  Detail: {self.detail}\n"
            f"  Remediation: {self.remediation}"
        )


async def check_database_connectivity(database_url: str) -> None:
    """ARCH-057 step 2: verify PostgreSQL connectivity.

    Runs a trivial SELECT 1 against the database. Raises
    StartupValidationError if the connection fails.
    """
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(database_url, pool_pre_ping=False)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
    except Exception as exc:
        raise StartupValidationError(
            step="database_connectivity",
            detail=str(exc),
            remediation=(
                "Check that VEKTRA_DATABASE_URL is correct and the PostgreSQL "
                "service is running. Docker Compose: `docker compose up postgres -d`."
            ),
        ) from exc


#: the remediation for a database recorded at a revision this release does not know (the
#: assess revisions 0009-0044 squashed into a0001, say): never "alembic upgrade", which cannot
#: locate it either
RECREATE_REMEDIATION = (
    "This release cannot migrate that database: re-create it (drop the database or its "
    "volume, then start again), as the upgrade note says (CHANGELOG, and "
    "docs/guides/operators/assess-deployment.md, 'Development databases migrated by the old "
    "chain')."
)


def _upgrade_remediation(branches: tuple[str, ...]) -> str:
    return "Run: " + " && ".join(f"alembic upgrade {b}@head" for b in branches)


def schema_problem(
    script: Any, current: set[str], branches: tuple[str, ...]
) -> StartupValidationError | None:
    """The decision of :func:`check_database_schema`, without the database.

    ``script`` is Alembic's ``ScriptDirectory``, ``current`` the revisions recorded in
    ``alembic_version``. A required head is applied when it is a current revision or one a
    current revision depends on (Alembic records only ``a0001`` once the assess base, which
    depends on the platform's head, is applied). Only Alembic's own resolution errors mean
    "unknown"; anything else propagates.
    """
    from alembic.script.revision import ResolutionError
    from alembic.util import CommandError

    expected: set[str] = set()
    for branch in branches:
        try:
            head = script.get_revision(f"{branch}@head")
        except (CommandError, ResolutionError):
            head = None
        if head is None:
            return StartupValidationError(
                step="database_schema",
                detail=f"no such migration branch in this release: {branch!r}",
                remediation=(
                    "Check alembic.ini's version_locations: the platform branch and, with "
                    "VEKTRA_ASSESS_ENABLED=true, the assess branch must both be present."
                ),
            )
        expected.add(head.revision)

    if not current:
        return StartupValidationError(
            step="database_schema",
            detail="No Alembic revisions applied (database is uninitialized).",
            remediation=_upgrade_remediation(branches),
        )
    unknown: set[str] = set()
    for rev in current:
        try:
            script.get_revision(rev)
        except (CommandError, ResolutionError):
            unknown.add(rev)
    if unknown:
        return StartupValidationError(
            step="database_schema",
            detail=(
                f"Schema is at revision(s) {sorted(current)!r}; unknown to this release: "
                f"{sorted(unknown)!r}."
            ),
            remediation=RECREATE_REMEDIATION,
        )
    applied = {r.revision for r in script.iterate_revisions(tuple(current), "base")}
    missing = expected - applied
    if missing:
        return StartupValidationError(
            step="database_schema",
            detail=(
                f"Schema is at revision(s) {sorted(current)!r}, but the required "
                f"head(s) are {sorted(expected)!r} (branches {list(branches)!r}). "
                f"Pending: {sorted(missing)!r}."
            ),
            remediation=_upgrade_remediation(branches),
        )
    return None


async def check_database_schema(
    database_url: str,
    alembic_ini: str = "alembic.ini",
    *,
    branches: tuple[str, ...] = ("platform",),
) -> None:
    """ARCH-057 step 3: verify that every required Alembic branch is at its head.

    The migrations are two branches (ADR-0033 D7): ``platform``, always required, and
    ``assess``, required only where assess is enabled (the caller passes it). A known branch
    that is not required may be present too: a deployment that turned assess off keeps its
    schema. The decision is :func:`schema_problem`.
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy.ext.asyncio import create_async_engine

        script = ScriptDirectory.from_config(Config(alembic_ini))
        engine = create_async_engine(database_url)

        def _get_current(conn: Connection) -> set[str]:
            mc = MigrationContext.configure(conn)
            return set(mc.get_current_heads())

        async with engine.connect() as conn:
            current = await conn.run_sync(_get_current)
        await engine.dispose()
    except Exception as exc:
        raise StartupValidationError(
            step="database_schema",
            detail=str(exc),
            remediation=(
                "Ensure Alembic is installed and alembic.ini with the migrations/ directory "
                "is present, and that the database is reachable. "
                + _upgrade_remediation(branches)
            ),
        ) from exc
    problem = schema_problem(script, current, branches)
    if problem is not None:
        raise problem
