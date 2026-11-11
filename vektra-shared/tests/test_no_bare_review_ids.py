"""Production code does not cite bare review ids.

A comment such as ``(CodeRabbit 4498473548)`` or ``(review 5812478913)`` points at a review
thread on a hosting service: the number says nothing to a reader of the code, and the thread can
be gone tomorrow. The comment must carry the reason itself. Test files may keep them (a
regression test naming the finding it pins is a different case), so the perimeter is every
tracked file under a ``src`` directory, outside any ``tests`` directory.

The perimeter is read from ``git ls-files``, so an untracked scratch file cannot make the guard
pass or fail, and a missing git is a failure, never a silent pass.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]

BARE_REVIEW_ID = re.compile(r"CodeRabbit [0-9]{6,}|[Rr]eview [0-9]{6,}")
_TEXT_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".jsx", ".tsx", ".html", ".css"}


def _perimeter() -> list[PurePosixPath]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    paths = [PurePosixPath(p) for p in out.split("\0") if p]
    return [
        p
        for p in paths
        if "src" in p.parts[:-1]
        and "tests" not in p.parts
        and p.suffix in _TEXT_SUFFIXES
        and not p.name.endswith((".min.js", ".min.css"))
    ]


def offences(text: str) -> list[tuple[int, str]]:
    return [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), start=1)
        if BARE_REVIEW_ID.search(line)
    ]


def test_the_perimeter_is_not_empty() -> None:
    """False-pass guard: an empty perimeter would pass for free."""
    files = _perimeter()
    assert len(files) >= 200
    assert any(p.parts[0] == "vektra-assess" and "ui" in p.parts for p in files)
    assert any(p.parts[0] == "vektra-admin" for p in files)


def test_the_matcher_finds_the_forms_it_exists_for() -> None:
    bad = (
        "# x (CodeRabbit 4498473548)\n"
        "# y (CodeRabbit review 5808100754)\n"
        "// z (review 5812478913)\n"
        '"""Review 1234567 said so."""\n'
    )
    assert [n for n, _ in offences(bad)] == [1, 2, 3, 4]
    fine = "# review fix 6\n# plan 13a\n# REQ-071, ERR-ASSESS-009, revision 0034\n"
    assert offences(fine) == []


def test_no_production_file_cites_a_bare_review_id() -> None:
    found = {
        f"{p}:{n}": line
        for p in _perimeter()
        for n, line in offences((REPO_ROOT / p).read_text(encoding="utf-8"))
    }
    assert found == {}, (
        "production code cites review ids; keep the reason, drop the number"
    )
