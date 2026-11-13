"""The assess migration branch, statically (ADR-0033 D7, plan 2a-3).

What the branch must be, read from its files without a database: one base revision on the
``assess`` branch depending on the platform's 0009, naming no LMS (the LMS boundary guard's own
matcher), carrying the rendered grant matrix, and refusing a migration user without CREATEROLE
with a message that names all three roles. The database side is
``integration/test_schema_branch.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from vektra_assess.db import grants as g

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "vektra-assess" / "src" / "vektra_assess" / "migrations" / "versions"
GUARD = ROOT / "vektra-shared" / "tests" / "test_lms_boundary.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def _revisions() -> list[Path]:
    return sorted(VERSIONS.glob("*.py"))


def test_one_base_revision_on_the_assess_branch() -> None:
    revs = _revisions()
    assert [p.name for p in revs] == [
        "a0001_assess_base.py",
        "a0004_assess_subject_indexes.py",  # the subject export's per-subject reads
    ]
    mod = _load(revs[0], "assess_base")
    assert mod.revision == "a0001"
    assert mod.down_revision is None
    assert mod.branch_labels == ("assess",)
    assert mod.depends_on == "0009"
    # every later revision is a straight line on the branch, labelled by the base only
    previous = "a0001"
    for path in revs[1:]:
        later = _load(path, f"assess_{path.stem}")
        assert later.down_revision == previous, path.name
        assert later.branch_labels is None and later.depends_on is None, path.name
        previous = later.revision


def test_the_assess_branch_names_no_lms() -> None:
    """The guard exempts ``vektra-assess`` as a directory until 2a-4; the branch is held to the
    empty baseline already, with the guard's matcher (every spelling it knows)."""
    guard = _load(GUARD, "lms_guard")
    mentions = [
        m
        for p in VERSIONS.rglob("*.py")
        for m in guard.mentions_in(str(p), p.read_text())
    ]
    assert mentions == []
    # the matcher is not dead: the same call sees a planted name
    assert guard.mentions_in("x.py", "assess_sites.moodle_origin")


def test_the_grant_matrix_is_the_rendered_one() -> None:
    mod = _load(_revisions()[0], "assess_base_grants")
    assert list(mod.GRANT_STATEMENTS) == g.render_statements()


def test_the_createrole_message_names_all_three_roles() -> None:
    mod = _load(_revisions()[0], "assess_base_message")
    for role in g.ROLES:
        assert role in mod.CREATEROLE_MESSAGE
    assert "SET ROLE vektra_assess_definer" in mod.CREATEROLE_MESSAGE
    assert mod.CREATEROLE_MESSAGE.count("%") == 1  # the one RAISE placeholder: the role
    assert "'" not in mod.CREATEROLE_MESSAGE  # it is spliced into a SQL string literal
