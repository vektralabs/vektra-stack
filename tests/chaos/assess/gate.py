"""The chaos gate: every case ran, none skipped, and the invariant report is complete and clean.

Usage: ``python tests/chaos/assess/gate.py <junit.xml> <report-dir> <min-cases> <min-scenarios>``

A skipped chaos case is a hole, not a pass (no Docker would skip every one of them), and a run
whose invariant report is missing or shorter than declared proves nothing. Exit 0 only when the
JUnit file has at least ``min-cases`` cases with no skip, failure or error, and the report has at
least ``min-scenarios`` distinct scenario names (negative controls and self-tests not
counted), every one ``ok``. The report is rewritten as a markdown summary next to the JSON lines
(the CI artifact).
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def evaluate(
    junit: Path, report_dir: Path, min_cases: int, min_scenarios: int
) -> list[str]:
    problems: list[str] = []
    if not junit.exists():
        return [f"{junit} missing: the chaos suite did not run"]
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
    failed = [
        c.get("name")
        for c in cases
        if c.find("failure") is not None or c.find("error") is not None
    ]
    if len(cases) < min_cases:
        problems.append(f"{len(cases)} cases collected, at least {min_cases} declared")
    if skipped:
        problems.append(f"skipped: {skipped}")
    if failed:
        problems.append(f"failed: {failed}")
    lines_path = report_dir / "invariants.jsonl"
    rows = (
        [json.loads(line) for line in lines_path.read_text().splitlines() if line]
        if lines_path.exists()
        else []
    )
    # only real scenarios count towards the declared minimum, each name once: the negative
    # controls and the checker's self-tests are not scenarios, and a scenario reported twice
    # is still one scenario
    scenarios = {str(r["scenario"]) for r in rows if not _negative(r)}
    if len(scenarios) < min_scenarios:
        problems.append(
            f"{len(scenarios)} invariant reports, at least {min_scenarios} declared"
        )
    bad = [r["scenario"] for r in rows if not r["ok"] and not _negative(r)]
    if bad:
        problems.append(f"invariant violations in: {bad}")
    _summary(report_dir, rows, problems)
    return problems


#: rows that are not chaos scenarios: negative controls (meant to report violations) and the
#: checker's own self-tests
NOT_SCENARIOS = ("negative:", "selftest:")


def _negative(row: dict[str, object]) -> bool:
    """A negative control or a checker self-test, never a scenario."""
    return str(row["scenario"]).startswith(NOT_SCENARIOS)


def _summary(
    report_dir: Path, rows: list[dict[str, object]], problems: list[str]
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    out = ["# Assess chaos invariant report", ""]
    out.append(f"Verdict: {'FAIL' if problems else 'PASS'}")
    out += [f"- {p}" for p in problems]
    out += ["", "| Scenario | Sessions | Declared | Result |", "|---|---|---|---|"]
    for r in rows:
        if _negative(r):
            continue
        out.append(
            f"| {r['scenario']} | {r['sessions']} | {r['expected_sessions']} | "
            f"{'ok' if r['ok'] else 'VIOLATION ' + json.dumps(r['violations'])} |"
        )
    (report_dir / "invariants.md").write_text("\n".join(out) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    problems = evaluate(Path(argv[1]), Path(argv[2]), int(argv[3]), int(argv[4]))
    for p in problems:
        print(f"assess chaos gate: {p}", file=sys.stderr)
    print("assess chaos gate:", "FAIL" if problems else "PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
