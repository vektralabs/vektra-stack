"""Unit tests for StartupValidationError (vektra_shared.startup).

The async functions check_database_connectivity and check_database_schema
depend on live PostgreSQL and Alembic internals - they are covered by
integration tests (test_migration.py). This file tests only the pure
StartupValidationError class.
"""

from __future__ import annotations

from vektra_shared.startup import StartupValidationError


class TestStartupValidationError:
    def test_fields_stored(self) -> None:
        err = StartupValidationError(
            step="database_connectivity",
            detail="connection refused",
            remediation="Check VEKTRA_DATABASE_URL",
        )
        assert err.step == "database_connectivity"
        assert err.detail == "connection refused"
        assert err.remediation == "Check VEKTRA_DATABASE_URL"

    def test_str_includes_step_and_detail(self) -> None:
        err = StartupValidationError(
            step="embedding_model", detail="wrong dims", remediation="fix model"
        )
        msg = str(err)
        assert "embedding_model" in msg
        assert "wrong dims" in msg

    def test_to_plain_text_format(self) -> None:
        err = StartupValidationError(
            step="database_schema",
            detail="schema behind",
            remediation="Run: alembic upgrade head",
        )
        text = err.to_plain_text()
        assert "[STARTUP ERROR]" in text
        assert "Step: database_schema" in text
        assert "Detail: schema behind" in text
        assert "Remediation: Run: alembic upgrade head" in text
