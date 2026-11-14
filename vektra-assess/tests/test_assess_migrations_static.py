"""The assess migration branch, statically (ADR-0033 D7, plan 2a-3).

What the branch must be, read from its files without a database: one base revision on the
``assess`` branch depending on the platform's 0009 (then a straight line of later revisions:
``a0002``, RLS for the API role), naming no LMS (the LMS boundary guard's own matcher), the
newest matrix-carrying revision carrying the rendered grant matrix, and refusing a migration user
without CREATEROLE with a message that names all three roles. The database side is
``integration/test_schema_branch.py`` and ``integration/test_api_rls.py``.
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
        "a0002_assess_api_rls.py",  # ADR-0033 D9.2: RLS for the API role
        "a0003_assess_erasure_alerts.py",  # OQ-033: the overdue-erasure alert marker
        "a0004_assess_subject_indexes.py",  # the subject export's per-subject reads
        "a0005_assess_outage_session_index.py",  # the stream's per-poll outage reads
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


def _latest_grants() -> Path:
    holders = [p for p in _revisions() if "GRANT_STATEMENTS = (" in p.read_text()]
    assert holders, "no revision carries the grant matrix"
    return holders[-1]


def test_the_rls_revision_scans_pending_replies_with_the_claim_rule_of_the_api() -> (
    None
):
    """``assess_pending_replies`` (a0002) carries the live-reply claim test as text: it must be
    the one ``turn_markers.reply_claimed_sql`` writes, or the PendingTurnDriver would answer a
    turn another driver holds."""
    from vektra_assess.turn_markers import reply_claimed_sql

    mod = _load(VERSIONS / "a0002_assess_api_rls.py", "assess_rls_claim")
    (pending,) = [f for f in mod.FUNCTIONS if "assess_pending_replies" in f]
    assert "AND NOT " + reply_claimed_sql("s.id") in pending


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
    """The newest revision that carries the matrix carries today's rendering of it."""
    mod = _load(_latest_grants(), "assess_latest_grants")
    assert list(mod.GRANT_STATEMENTS) == g.render_statements()


def test_the_createrole_message_names_all_three_roles() -> None:
    mod = _load(_revisions()[0], "assess_base_message")
    for role in g.ROLES:
        assert role in mod.CREATEROLE_MESSAGE
    assert "SET ROLE vektra_assess_definer" in mod.CREATEROLE_MESSAGE
    assert mod.CREATEROLE_MESSAGE.count("%") == 1  # the one RAISE placeholder: the role
    assert "'" not in mod.CREATEROLE_MESSAGE  # it is spliced into a SQL string literal
