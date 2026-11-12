"""``assess_sites.lms_origin`` and its CHECK (plan 9, review #8; formerly revision 0033).

The CHECK ``ck_assess_sites_lms_origin`` must accept every origin the code's normalisation
(``ui.origins.url_origin``) writes, and refuse what it would never write (a path, credentials,
upper case). A ``base_url`` the code cannot normalise has no origin at all (``None``): the column
stays NULL and ``site_origin`` refuses the site the same way.

Runs against a real Postgres (testcontainers), at the assess head. Marked ``integration``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

#: base_url -> the normalised origin the code derives from it (None: no usable origin)
CASES = {
    "https://lms.example.org/lms": "https://lms.example.org",
    "HTTPS://L.Example.org:443/x/": "https://l.example.org",
    "http://lms.example.org:80": "http://lms.example.org",
    "https://lms.example.org:8443/": "https://lms.example.org:8443",
    "https://[::1]:8443/lms": "https://[::1]:8443",
    "https://[2001:DB8:0:0:0:0:0:1]/": "https://[2001:db8::1]",
    "https://lms_x.example.org/": None,  # underscore: not a host name
    "https://user:secret@lms.example.org/": None,  # credentials
    "https://user@lms.example.org/": None,
    "http://[fe80::1%25eth0]/lms": None,  # an IPv6 zone id (CodeRabbit 4501694787)
}


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VEKTRA_DATABASE_URL": url}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )


async def _run(url: str, sql: str, params: dict | None = None):  # type: ignore[no-untyped-def,type-arg]
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def pg():  # type: ignore[no-untyped-def]
    docker = pytest.importorskip("docker", reason="docker SDK not installed")
    try:
        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Docker not available: {exc}")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as c:
        url = c.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        res = _alembic(url, "upgrade", "heads")
        assert res.returncode == 0, res.stderr
        yield url


async def _site(url: str, base_url: str, origin: str | None) -> uuid.UUID:
    site = uuid.uuid4()
    await _run(
        url,
        "INSERT INTO assess_sites (id, lms_site_uuid, base_url, lms_origin) "
        "VALUES (:i, :u, :b, :o)",
        {"i": site, "u": str(site), "b": base_url, "o": origin},
    )
    return site


async def test_the_check_accepts_every_origin_the_code_writes(pg) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.ui.origins import site_origin, url_origin

    for base_url, want in CASES.items():
        assert url_origin(base_url) == want, base_url  # the code's own normalisation
        site = await _site(pg, base_url, want)
        got = (
            await _run(
                pg, "SELECT lms_origin FROM assess_sites WHERE id = :i", {"i": site}
            )
        )[0][0]
        assert got == want, base_url
        assert site_origin(got, base_url) == want  # the fallback agrees on NULL rows


async def test_the_check_takes_normalised_origins_only(pg) -> None:  # type: ignore[no-untyped-def]
    site = await _site(pg, "https://lms.example.org/lms", None)
    for good in (
        "https://[::1]:8443",
        "http://127.0.0.1:8080",
        "https://a-b.example.org",
    ):
        await _run(
            pg,
            "UPDATE assess_sites SET lms_origin = :o WHERE id = :i",
            {"o": good, "i": site},
        )
    for bad in (
        "https://l.example.org/x",
        "https://u:p@l.example.org",
        "HTTPS://L.EXAMPLE.ORG",
    ):
        with pytest.raises(Exception, match="ck_assess_sites_lms_origin"):
            await _run(
                pg,
                "UPDATE assess_sites SET lms_origin = :o WHERE id = :i",
                {"o": bad, "i": site},
            )
