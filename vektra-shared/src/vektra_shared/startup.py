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
    database_url: str, migrations_path: str = "migrations"
) -> None:
    """ARCH-057 step 3: verify Alembic schema is at current head.

    Raises StartupValidationError if the deployed schema is behind the
    expected migration head, meaning `alembic upgrade head` is needed.
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy.ext.asyncio import create_async_engine

        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", migrations_path)
        script = ScriptDirectory.from_config(alembic_cfg)
        heads = {rev.revision for rev in script.get_revisions("heads")}

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
                remediation="Run: alembic upgrade head",
            )
        if current != heads:
            pending = heads - current
            raise StartupValidationError(
                step="database_schema",
                detail=(
                    f"Schema is at revision(s) {current!r}, "
                    f"but head is {heads!r}. Pending: {pending!r}."
                ),
                remediation="Run: alembic upgrade head",
            )
    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step="database_schema",
            detail=str(exc),
            remediation=(
                "Ensure Alembic is installed and migrations/ directory is present. "
                "Run: alembic upgrade head"
            ),
        ) from exc
