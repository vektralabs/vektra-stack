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


def _script():  # type: ignore[no-untyped-def]
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    return ScriptDirectory.from_config(Config(str(ini)))


def _problem(current: set[str], branches: tuple[str, ...]):  # type: ignore[no-untyped-def]
    from vektra_shared.startup import schema_problem

    return schema_problem(_script(), current, branches)


class TestSchemaProblem:
    """The decision of ARCH-057 step 3, on the real migration scripts and a given set of
    current revisions (no database: ``check_database_schema`` reads the set, then calls
    this). Review of #206: unknown revisions, an unknown branch, and each remediation."""

    def test_heads_applied_is_no_problem(self) -> None:
        script = _script()
        platform = script.get_revision("platform@head").revision
        assessment = script.get_revision("assess@head").revision
        assert _problem({platform}, ("platform",)) is None
        assert _problem({assessment}, ("platform", "assess")) is None  # implied
        assert _problem({assessment}, ("platform",)) is None  # assess turned off

    def test_a_pending_branch_says_which_upgrade_to_run(self) -> None:
        platform = _script().get_revision("platform@head").revision
        err = _problem({platform}, ("platform", "assess"))
        assert err is not None
        assert "alembic upgrade assess@head" in err.remediation

    def test_an_unknown_revision_says_recreate_never_upgrade(self) -> None:
        err = _problem({"0044"}, ("platform",))
        assert err is not None
        assert "unknown to this release: ['0044']" in err.detail
        assert "re-create" in err.remediation.lower()
        assert "upgrade note" in err.remediation
        assert "alembic upgrade" not in err.remediation

    def test_an_unknown_branch_is_a_clear_error(self) -> None:
        err = _problem({"0001"}, ("platform", "nosuch"))
        assert err is not None
        assert "nosuch" in err.detail and "no such migration branch" in err.detail

    def test_only_alembic_resolution_errors_are_caught(self) -> None:
        """A bug elsewhere is not reported as an 'unknown revision'."""
        import pytest

        class Broken:
            def get_revision(self, rev: str) -> object:
                raise ZeroDivisionError

        from vektra_shared.startup import schema_problem

        with pytest.raises(ZeroDivisionError):
            schema_problem(Broken(), {"0001"}, ("platform",))
