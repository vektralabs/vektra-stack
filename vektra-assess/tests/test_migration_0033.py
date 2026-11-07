"""Migration 0033: ``assess_sites.moodle_origin`` backfill (plan 9, review #8).

The backfill must use the same normalisation as the code (``ui.origins.url_origin``) and the
CHECK must accept every value it writes: a ``base_url`` it cannot normalise (credentials, a host
with an underscore) is backfilled as NULL, never left to fail the migration; the code then falls
back to ``base_url`` (``site_origin``), which refuses it the same way.

Runs against a real Postgres (testcontainers), from 0032 to 0033. Marked ``integration``.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

#: base_url -> the backfilled moodle_origin
CASES = {
    "https://moodle.example/moodle": "https://moodle.example",
    "HTTPS://M.Example:443/x/": "https://m.example",
    "http://moodle.example:80": "http://moodle.example",
    "https://moodle.example:8443/": "https://moodle.example:8443",
    "https://[::1]:8443/moodle": "https://[::1]:8443",
    "https://[2001:DB8:0:0:0:0:0:1]/": "https://[2001:db8::1]",
    "https://moodle_x.example/": None,  # underscore: not a host name
    "https://user:secret@moodle.example/": None,  # credentials
    "https://user@moodle.example/": None,
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
def pg_0032():  # type: ignore[no-untyped-def]
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
        res = _alembic(url, "upgrade", "0032")
        assert res.returncode == 0, res.stderr
        yield url


async def test_the_backfill_normalises_like_the_code_or_writes_null(pg_0032) -> None:  # type: ignore[no-untyped-def]
    ids = {}
    for base_url in CASES:
        site = uuid.uuid4()
        ids[base_url] = site
        await _run(
            pg_0032,
            "INSERT INTO assess_sites (id, moodle_site_uuid, base_url) VALUES (:i, :u, :b)",
            {"i": site, "u": str(site), "b": base_url},
        )
    res = _alembic(pg_0032, "upgrade", "0033")
    assert res.returncode == 0, res.stderr
    from vektra_assess.ui.origins import site_origin, url_origin

    for base_url, want in CASES.items():
        got = (
            await _run(
                pg_0032,
                "SELECT moodle_origin FROM assess_sites WHERE id = :i",
                {"i": ids[base_url]},
            )
        )[0][0]
        assert got == want, base_url
        assert got == url_origin(base_url), base_url  # the code's own normalisation
        assert site_origin(got, base_url) == want  # the fallback agrees on NULL rows

    # the CHECK takes normalised origins only (IPv6 included), and refuses the rest
    site = ids["https://moodle.example/moodle"]
    for good in ("https://[::1]:8443", "http://127.0.0.1:8080", "https://a-b.example"):
        await _run(
            pg_0032,
            "UPDATE assess_sites SET moodle_origin = :o WHERE id = :i",
            {"o": good, "i": site},
        )
    for bad in ("https://m.example/x", "https://u:p@m.example", "HTTPS://M.EXAMPLE"):
        with pytest.raises(Exception, match="ck_assess_sites_moodle_origin"):
            await _run(
                pg_0032,
                "UPDATE assess_sites SET moodle_origin = :o WHERE id = :i",
                {"o": bad, "i": site},
            )
