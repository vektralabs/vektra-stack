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

#: three forms: a labelled id (``CodeRabbit 4498473548``, ``review 5812478913``); an
#: unlabelled one, which is the shape of every review-thread id the hosting service has issued
#: here (ten digits starting with 4 or 5: ``(4504591109)``), standing alone so a hex hash or an
#: identifier that merely contains such digits is not one (a real constant of that size is
#: written with separators, ``4_294_967_295``); and, across lines, a label that ends one
#: comment line with its digits starting the next.
BARE_REVIEW_ID = re.compile(
    r"CodeRabbit [0-9]{6,}|[Rr]eview [0-9]{6,}|(?<![\w.])[45][0-9]{9}(?!\w)"
)
_LABEL_AT_END = re.compile(r"(?:CodeRabbit(?: review)?|[Rr]eview)\s*$")
_DIGITS_AT_START = re.compile(r"^\s*(?:#:?|//|\*)?\s*[0-9]{6,}")
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
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for n, line in enumerate(lines, start=1):
        split = (
            n < len(lines)
            and _LABEL_AT_END.search(line) is not None
            and _DIGITS_AT_START.match(lines[n]) is not None
        )
        if split or BARE_REVIEW_ID.search(line):
            found.append((n, line.strip()))
    return found


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
    unlabelled = (
        "# waits like the others (4504591109)\n"  # 1
        "// mints for a resume, 4501070014)\n"  # 2
        "x = 1\n"
        "# the other pools (CodeRabbit\n"  # 4: the label, split from its digits
        "# 123456 on the next line)\n"  # 5: six digits, caught only through line 4
        "    # reads the rejected value (CodeRabbit review\n"  # 6
        "    # 5808100754): generate\n"  # 7: also an unlabelled id by itself
    )
    assert [n for n, _ in offences(unlabelled)] == [1, 2, 4, 6, 7]
    fine = (
        "# review fix 6\n# plan 13a\n# REQ-071, ERR-ASSESS-009, revision 0034\n"
        'STYLE_SHA256 = "d435a52514347012e4cdb9d0cba58f3f1b5cdeb3c6aedd99aa0566599ee103c7"\n'
        '"seeded_at": 1793619221,\n'  # an epoch timestamp
        "MAX = 4_294_967_295\n"  # a constant written with separators
        "h = 0x4501070014\nv = 1.4501070014\nname_4501070014 = 1\n"
        "# 45010700141 has eleven digits\n"
        "# a code review\n# 2026 was the year\n"  # a label at the end, no id after it
    )
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
