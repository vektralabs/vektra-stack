"""Assess modules log through the allowlist (plan assess-context A)."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
import structlog

SRC = Path(__file__).resolve().parents[1] / "src" / "vektra_assess"
CANARY = "CNRY-solution_text-essay-0f0f0f0f"


def _calls(tree: ast.AST, attr: str, owner: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == owner
    ]


def _violations(src: Path = SRC) -> list[str]:
    bad = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for call in _calls(tree, "get_logger", "structlog"):
            # a bare get_logger() has no name, so the allowlist cannot tell it is an assess logger
            if not call.args:
                bad.append(
                    f"{path.relative_to(src)}:{call.lineno} structlog.get_logger()"
                )
        for call in _calls(tree, "getLogger", "logging"):
            arg = call.args[0] if call.args else None
            named = isinstance(arg, ast.Constant) and str(arg.value).startswith(
                "vektra_assess"
            )
            if arg is not None and not named and not isinstance(arg, ast.Name):
                bad.append(
                    f"{path.relative_to(src)}:{call.lineno} logging.getLogger(...)"
                )
    return bad


def test_every_assess_logger_carries_an_assess_name() -> None:
    assert _violations() == []


def test_the_guard_catches_a_bare_logger(tmp_path: Path) -> None:
    pkg = tmp_path / "vektra_assess"
    pkg.mkdir()
    (pkg / "planted.py").write_text("import structlog\nlog = structlog.get_logger()\n")
    assert _violations(pkg) == ["planted.py:2 structlog.get_logger()"]


def test_real_module_loggers_are_filtered(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    from vektra_assess.moodle import client  # a module that logs through structlog

    client.log.warning(
        "assess_probe", wsfunction="core_webservice_get_site_info", body=CANARY
    )
    logging.getLogger("vektra_assess.worker").info(
        "assess_tick", extra={"payload": CANARY}
    )
    structlog.get_logger("vektra_core.x").info("not_assess")
    canary_log_capture.assert_clean([CANARY])
    assert len(canary_log_capture.lines()) == 3


def test_capture_refuses_an_empty_sink(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(AssertionError, match="log sink is empty"):
        canary_log_capture.assert_clean([CANARY])


def test_capture_catches_extra_rendered_output(canary_log_capture) -> None:  # type: ignore[no-untyped-def]
    canary_log_capture.extra_text.append(f"sidecar stderr: {CANARY}\n")
    with pytest.raises(AssertionError, match="canary hit"):
        canary_log_capture.assert_clean([CANARY])
