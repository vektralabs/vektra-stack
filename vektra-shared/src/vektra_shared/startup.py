"""Startup validation utilities (ARCH-057).

Steps 2 and 3 of the startup validation sequence:
  2. Database connectivity check (asyncpg connection test)
  3. Database schema check (Alembic current head = deployed head)

Steps 1, 4-8 are implemented in each component's startup logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


async def check_database_schema(
    database_url: str,
    alembic_ini: str = "alembic.ini",
    *,
    branches: tuple[str, ...] = ("platform",),
) -> None:
    """ARCH-057 step 3: verify that every required Alembic branch is at its head.

    The migrations are two branches (ADR-0033 D7): ``platform``, always required, and
    ``assess``, required only where assess is enabled (the caller passes it). A required head
    is applied when it is a current revision or one a current revision depends on (Alembic
    records only ``a0001`` once the assess base, which depends on ``0008``, is applied). A
    known branch that is not required may be present too: a deployment that turned assess off
    keeps its schema. A required head that is not applied, or a revision this release does not
    know, fails the step.
    """
    remediation = "Run: " + " && ".join(f"alembic upgrade {b}@head" for b in branches)
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy.ext.asyncio import create_async_engine

        script = ScriptDirectory.from_config(Config(alembic_ini))
        expected: set[str] = set()
        for branch in branches:
            head = script.get_revision(f"{branch}@head")
            assert head is not None
            expected.add(head.revision)

        engine = create_async_engine(database_url)

        def _get_current(conn: Connection) -> set[str]:
            mc = MigrationContext.configure(conn)
            return set(mc.get_current_heads())

        async with engine.connect() as conn:
            current = await conn.run_sync(_get_current)
        await engine.dispose()

        if not current:
            raise StartupValidationError(
                step="database_schema",
                detail="No Alembic revisions applied (database is uninitialized).",
                remediation=remediation,
            )
        unknown: set[str] = set()
        for rev in current:
            try:
                script.get_revision(rev)
            except Exception:
                unknown.add(rev)
        applied = (
            set()
            if unknown
            else {r.revision for r in script.iterate_revisions(tuple(current), "base")}
        )
        missing = expected - applied
        if missing or unknown:
            raise StartupValidationError(
                step="database_schema",
                detail=(
                    f"Schema is at revision(s) {sorted(current)!r}, but the required "
                    f"head(s) are {sorted(expected)!r} (branches {list(branches)!r}). "
                    f"Pending: {sorted(missing)!r}; unknown to this release: "
                    f"{sorted(unknown)!r}."
                ),
                remediation=remediation,
            )
    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step="database_schema",
            detail=str(exc),
            remediation=(
                "Ensure Alembic is installed and alembic.ini with the migrations/ directory "
                "is present. " + remediation
            ),
        ) from exc
