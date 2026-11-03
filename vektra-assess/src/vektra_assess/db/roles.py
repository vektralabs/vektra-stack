"""Postgres role assertions for the assess processes (INT-002).

The assess API and worker each connect as their own NOSUPERUSER role. The check is
empirical: it asks the server who the session is, instead of trusting the DSN.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from vektra_shared.startup import StartupValidationError


def dsn_user(url: str) -> str | None:
    return make_url(url).username


def dsn_with_password(url: str, password: str, *, step: str) -> str:
    """Return ``url`` with the role password from its secret file.

    The configured DSN must carry no password: role passwords live only in secret
    files (``assess_db_*_password``), never in the environment or ``.env``.
    """
    parsed = make_url(url)
    if parsed.password:
        raise StartupValidationError(
            step=step,
            detail="the assess DSN contains a password",
            remediation=(
                "Remove the password from the DSN; it is read from the "
                "assess_db_api_password / assess_db_worker_password secret file."
            ),
        )
    return parsed.set(password=password).render_as_string(hide_password=False)


def assert_distinct_dsn(assess_url: str, main_url: str, *, step: str) -> None:
    """The assess DSN must not log in as the main (superuser) DSN's user."""
    a, m = dsn_user(assess_url), dsn_user(main_url)
    if a is None or a == m:
        raise StartupValidationError(
            step=step,
            detail=f"the assess DSN logs in as {a!r}, the same user as VEKTRA_DATABASE_URL",
            remediation="Point the assess DSN at its own role (vektra_assess_api / _worker).",
        )


async def assert_session_role(engine: AsyncEngine, expected: str, *, step: str) -> None:
    """Raise unless the session's ``current_user`` is ``expected`` and not superuser."""
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT current_user, r.rolsuper FROM pg_roles r "
                        "WHERE r.rolname = current_user"
                    )
                )
            ).one()
    except StartupValidationError:
        raise
    except Exception as exc:
        raise StartupValidationError(
            step=step,
            detail=f"cannot connect with the assess DSN: {type(exc).__name__}",
            remediation=f"Check that role {expected} exists with LOGIN (vektra-assess-db-init).",
        ) from exc
    user, superuser = row[0], bool(row[1])
    if user != expected or superuser:
        raise StartupValidationError(
            step=step,
            detail=f"assess DSN session is {user!r} (superuser={superuser}), expected {expected!r}",
            remediation=f"Use a DSN for role {expected}, which is NOSUPERUSER.",
        )
