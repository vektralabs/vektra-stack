"""Every error code the code defines has a row in ``docs/reference/error-codes.md`` (NFR-009).

The set of codes is derived, never listed by hand: every ``ERR_*`` constant of
``vektra_shared.errors`` plus every ``ERR-<COMPONENT>-<NNN>`` literal in a ``*/src`` Python file
(``ERR-ADMIN-001``..``007`` and ``ERR-SAFEGUARD-001`` are string literals in their component, the
same scan ``vektra-assess/tests/test_reference_docs.py`` does for the assess codes). A code named
under "Deliberately not listed" in the registry is exempt, so an exemption is written down where
a reader of the registry sees it.

A row must have a non-empty message and, in a table that has a Remediation column, a non-empty
remediation. The negative controls below show the check fails on a missing row and on an empty
remediation, so a green run is not empty == empty.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ERROR_DOC = ROOT / "docs" / "reference" / "error-codes.md"

_CODE = re.compile(r"ERR-[A-Z]+-\d{3}")
_EXEMPT_HEADING = "### Deliberately not listed"


def _defined_codes() -> dict[str, str]:
    """Code -> where it is defined (first occurrence)."""
    from vektra_shared import errors

    found: dict[str, str] = {
        value: f"vektra_shared.errors.{name}"
        for name, value in vars(errors).items()
        if name.startswith("ERR_") and isinstance(value, str)
    }
    for src in sorted(ROOT.glob("*/src")):
        for path in sorted(src.rglob("*.py")):
            for code in _CODE.findall(path.read_text(encoding="utf-8")):
                found.setdefault(code, str(path.relative_to(ROOT)))
    return found


def _exempt(text: str) -> set[str]:
    if _EXEMPT_HEADING not in text:
        return set()
    section = text.split(_EXEMPT_HEADING, 1)[1].split("\n#", 1)[0]
    return set(_CODE.findall(section))


def _rows(text: str) -> dict[str, dict[str, str]]:
    """Code -> {column header: cell} for every table row whose first cell is a code."""
    rows: dict[str, dict[str, str]] = {}
    header: list[str] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            header = []
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "Code":
            header = cells
            continue
        if not header or not _CODE.fullmatch(cells[0]):
            continue
        assert cells[0] not in rows, f"{cells[0]} has two rows"
        # a row shorter than its header (a remediation cell left out) must not read as a row
        # of a table without that column: pad, so the missing cell is an empty one
        rows[cells[0]] = dict(
            zip(header, cells + [""] * (len(header) - len(cells)), strict=False)
        )
    return rows


def _problems(defined: dict[str, str], text: str) -> list[str]:
    rows, exempt = _rows(text), _exempt(text)
    problems: list[str] = []
    for code, origin in sorted(defined.items()):
        if code in exempt:
            continue
        row = rows.get(code)
        if row is None:
            problems.append(f"{code} ({origin}) has no row")
            continue
        if not row.get("Message"):
            problems.append(f"{code} has an empty message")
        if "Remediation" in row and not row["Remediation"]:
            problems.append(f"{code} has an empty remediation")
    return problems


def test_every_defined_error_code_has_a_registry_row() -> None:
    assert _problems(_defined_codes(), ERROR_DOC.read_text(encoding="utf-8")) == []


def test_the_derived_code_set_is_not_trivially_small() -> None:
    """False-pass guard: a broken scan must not make the comparison vacuous."""
    defined = _defined_codes()
    assert len(defined) >= 70
    # one literal-only code, one constant, one assess code
    assert {"ERR-ADMIN-001", "ERR-NAMESPACE-001", "ERR-ASSESS-001"} <= set(defined)
    assert len(_rows(ERROR_DOC.read_text(encoding="utf-8"))) >= 70


def test_negative_control_missing_row_is_reported() -> None:
    text = ERROR_DOC.read_text(encoding="utf-8")
    defined = {"ERR-NOPE-999": "synthetic"}
    assert _problems(defined, text) == ["ERR-NOPE-999 (synthetic) has no row"]


def test_negative_control_empty_remediation_is_reported() -> None:
    text = (
        "| Code | Category | HTTP | Message | Remediation |\n"
        "|------|----------|------|---------|-------------|\n"
        "| ERR-NOPE-001 | PERMANENT | 400 | Something failed |  |\n"
    )
    assert _problems({"ERR-NOPE-001": "synthetic"}, text) == [
        "ERR-NOPE-001 has an empty remediation"
    ]


def test_negative_control_omitted_remediation_cell_is_reported() -> None:
    """The row stops after its message: no Remediation cell at all, not an empty one."""
    text = (
        "| Code | Category | HTTP | Message | Remediation |\n"
        "|------|----------|------|---------|-------------|\n"
        "| ERR-NOPE-005 | PERMANENT | 400 | Something failed |\n"
    )
    assert _problems({"ERR-NOPE-005": "synthetic"}, text) == [
        "ERR-NOPE-005 has an empty remediation"
    ]


def test_negative_control_exemption_is_scoped_to_its_section() -> None:
    text = (
        "| Code | Component | HTTP | Message |\n|---|---|---|---|\n"
        "| ERR-NOPE-002 | x | 400 | listed |\n\n"
        f"{_EXEMPT_HEADING}\n\n`ERR-NOPE-003` is not documented yet.\n\n"
        "## Next section\n\n`ERR-NOPE-004` is mentioned here.\n"
    )
    defined = dict.fromkeys(("ERR-NOPE-002", "ERR-NOPE-003", "ERR-NOPE-004"), "s")
    assert _problems(defined, text) == ["ERR-NOPE-004 (s) has no row"]
