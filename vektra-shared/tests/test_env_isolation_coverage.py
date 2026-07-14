"""Every test package must be isolated from the local environment (DEBT-029).

A structural test, on purpose. The behavioural one (`vektra-core/tests/
test_env_isolation.py`) proves the mechanism works where it is wired up; this one
proves it is wired up **everywhere**, so that a package added tomorrow cannot quietly
inherit the gap that let BUG-024 through — five of the eight test packages had no
conftest at all, and the three that did were not protected either.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _test_packages() -> list[Path]:
    return sorted(p for p in REPO_ROOT.glob("vektra-*/tests") if p.is_dir())


def test_the_test_packages_are_discovered() -> None:
    """Guard the guard: a glob that matches nothing would make this file vacuous."""
    assert len(_test_packages()) >= 8


def test_every_test_package_imports_the_isolation_fixture() -> None:
    missing = []
    for tests_dir in _test_packages():
        conftest = tests_dir / "conftest.py"
        if not conftest.is_file():
            missing.append(f"{tests_dir.relative_to(REPO_ROOT)}: no conftest.py")
            continue
        if "vektra_shared.testing" not in conftest.read_text(encoding="utf-8"):
            missing.append(
                f"{conftest.relative_to(REPO_ROOT)}: does not import "
                "the shared isolation fixture"
            )

    assert missing == [], (
        "these test packages read whatever the developer has in their .env:\n  "
        + "\n  ".join(missing)
    )
