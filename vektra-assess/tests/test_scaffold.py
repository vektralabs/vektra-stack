"""Smoke test: every vektra_assess sub-package and module imports on its own."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import vektra_assess

PKG_DIR = Path(vektra_assess.__file__).resolve().parent

EXPECTED_SUBPACKAGES = {
    "api",
    "privacy",  # plan assess-privacy-retention: config, purge, erasure
    "worker",
    "moodle",
    "parser_sidecar",
    "context",
    "llm",
    "transcript",
    "delivery",
    "crypto",
    "db",
    "ui",  # the discussion UI page, its headers and origins (plan 9)
    "teacher",  # the config request models and BR-013 rules, pure (plan 11)
    "setup",  # the REQ-079 setup-check runner and Vektra's own items (ADR-0033 D3)
}
EXPECTED_MODULES = {"config", "keys", "errors", "health", "parser_protocol"}


def _children() -> tuple[set[str], set[str]]:
    pkgs, mods = set(), set()
    for info in pkgutil.iter_modules([str(PKG_DIR)]):
        (pkgs if info.ispkg else mods).add(info.name)
    return pkgs, mods


def test_layout_matches_design() -> None:
    pkgs, mods = _children()
    assert pkgs == EXPECTED_SUBPACKAGES
    assert EXPECTED_MODULES <= mods


@pytest.mark.parametrize("name", sorted(EXPECTED_SUBPACKAGES | EXPECTED_MODULES))
def test_child_imports(name: str) -> None:
    mod = importlib.import_module(f"vektra_assess.{name}")
    assert mod.__doc__, f"vektra_assess.{name} has no docstring"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names)
    return found


def test_subpackage_init_imports_no_sibling() -> None:
    """No sub-package ``__init__`` imports another sub-package (partial graph stays partial)."""
    offenders = []
    for name in EXPECTED_SUBPACKAGES:
        imported = _imported_modules(PKG_DIR / name / "__init__.py")
        for other in EXPECTED_SUBPACKAGES - {name}:
            if any(
                m.startswith((f"vektra_assess.{other}", f"..{other}"))
                or m == f"...{other}"
                for m in imported
            ):
                offenders.append(f"{name} -> {other}")
    assert offenders == []


def test_sibling_detector_is_not_vacuous(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("from vektra_assess.moodle import x\nfrom ..crypto import y\n")
    imported = _imported_modules(probe)
    assert "vektra_assess.moodle" in imported
    assert "..crypto" in imported
