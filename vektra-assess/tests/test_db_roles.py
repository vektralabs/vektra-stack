"""Role assertion helpers (unit; the real-Postgres path is in vektra-app integration tests)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from vektra_assess.db.roles import assert_distinct_dsn, assert_session_role, dsn_user
from vektra_shared.startup import StartupValidationError


class _Result:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def one(self) -> tuple[Any, ...]:
        return self._row


class _Engine:
    def __init__(
        self, row: tuple[Any, ...] | None = None, error: Exception | None = None
    ) -> None:
        self.row, self.error = row, error

    @asynccontextmanager
    async def connect(self):  # type: ignore[no-untyped-def]
        if self.error:
            raise self.error
        engine = self

        class _Conn:
            async def execute(self, _stmt):  # type: ignore[no-untyped-def]
                return _Result(engine.row)  # type: ignore[arg-type]

        yield _Conn()


def test_dsn_user_and_distinct() -> None:
    assert (
        dsn_user("postgresql+asyncpg://vektra_assess_api:pw@db/vektra")
        == "vektra_assess_api"
    )
    assert_distinct_dsn("postgresql://a:x@db/v", "postgresql://vektra:y@db/v", step="s")
    with pytest.raises(StartupValidationError, match="same user"):
        assert_distinct_dsn(
            "postgresql://vektra:x@db/v", "postgresql://vektra:y@db/v", step="s"
        )
    with pytest.raises(StartupValidationError):
        assert_distinct_dsn("postgresql://db/v", "postgresql://vektra:y@db/v", step="s")


async def test_session_role_ok() -> None:
    await assert_session_role(
        _Engine(("vektra_assess_api", False)), "vektra_assess_api", step="s"
    )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("row", "match"),
    [
        (("vektra", True), "superuser=True"),
        (("other", False), "'other'"),
        (("vektra_assess_api", True), "superuser=True"),
    ],
)
async def test_session_role_refusals(row: tuple[str, bool], match: str) -> None:
    with pytest.raises(StartupValidationError, match=match):
        await assert_session_role(_Engine(row), "vektra_assess_api", step="s")  # type: ignore[arg-type]


async def test_connection_failure_is_a_startup_error_without_secrets() -> None:
    with pytest.raises(StartupValidationError) as exc:
        await assert_session_role(
            _Engine(error=OSError("password=hunter2 refused")), "r", step="s"
        )  # type: ignore[arg-type]
    assert "hunter2" not in exc.value.detail and "OSError" in exc.value.detail
