"""Every test suite must actually be executed by something (DEBT-031).

`test_env_isolation_coverage.py` proves each test package is *isolated*. It does not
prove anyone *runs* it. Nothing did: `make test` and `ci-unit.yml` each enumerate the
packages by hand, in two independent lists, so a suite absent from both is silently
never executed — and no test goes red. That is the hole BUG-024 fell through
(`vektra-app/tests/` was run by neither), and when DEBT-030 finally ran those files it
turned out they had never worked at all. A test nobody runs does not stay neutral, it
rots.

So this guard reads the runners instead of trusting them: the `test:` recipe in the
Makefile, and every `pytest` invocation in every workflow. For each test file it asks
whether some runner would actually *collect* it, which means honouring the `-m` filter
as well as the paths — a file can sit in a directory both runners name and still be
executed by neither, because every unit run passes `-m "not integration"`. That is not
hypothetical: it is exactly how the integration suites of vektra-admin, vektra-index
and vektra-ingest were never once executed.

The unit path and the integration path are therefore checked independently.

Parsing is enough; nothing here executes a suite. Workflow YAML is parsed, never
grepped: a commented-out `pytest` line is a comment, and a guard that counted it as
coverage would certify a suite that no one runs.
"""

from __future__ import annotations

import ast
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The `-m` expressions this guard knows how to reason about. An unrecognised one is a
# hard failure rather than a silent "covers nothing" / "covers everything" guess: the
# whole point is not to certify coverage we cannot actually demonstrate.
_SELECTS_UNIT = {None: True, "not integration": True, "integration": False}
_SELECTS_INTEGRATION = {None: True, "not integration": False, "integration": True}

# Suites deliberately run by nothing. Every entry needs a reason, and the entry has to
# stay true: a stale one fails below. Adding to this list is how you *knowingly* stop
# running a suite, which is a thing a reviewer should see you do.
EXPECTED_UNRUN: dict[str, str] = {
    "tests/nfr/test_performance.py": (
        "measures query latency against a live LLM, which GitHub Actions does not "
        "have; run manually on Kalypso (see the note in integration.yml)"
    ),
}


# ---------------------------------------------------------------------------
# What tests exist, and what kind they are
# ---------------------------------------------------------------------------


def _mark_name(node: ast.expr) -> str | None:
    """The `x` of a `pytest.mark.x` node, with or without a call."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
        if node.value.attr == "mark":
            return node.attr
    return None


def _module_marks(tree: ast.Module) -> set[str]:
    """Marks applied to the whole module via a top-level `pytestmark`."""
    marks: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        values = (
            node.value.elts
            if isinstance(node.value, ast.List | ast.Tuple)
            else [node.value]
        )
        marks |= {name for v in values if (name := _mark_name(v))}
    return marks


@dataclass(frozen=True)
class Suite:
    """A test file, and the kinds of test it holds."""

    path: Path
    has_unit: bool
    has_integration: bool

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))


def _classify(path: Path) -> Suite:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    if "integration" in _module_marks(tree):
        # A module-level mark applies to every test in the file, so there is nothing
        # left that a unit run could pick up.
        return Suite(path, has_unit=False, has_integration=True)

    has_unit = False
    has_integration = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if any(_mark_name(d) == "integration" for d in node.decorator_list):
            has_integration = True
        else:
            has_unit = True
    return Suite(path, has_unit=has_unit, has_integration=has_integration)


def _suites() -> list[Suite]:
    roots = [*REPO_ROOT.glob("vektra-*/tests"), REPO_ROOT / "tests"]
    files = {
        f
        for root in roots
        if root.is_dir()
        for f in root.rglob("test_*.py")
        if "__pycache__" not in f.parts
    }
    return sorted((_classify(f) for f in files), key=lambda s: s.rel)


# ---------------------------------------------------------------------------
# What the runners actually run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """One `pytest ...` command found in a runner."""

    source: str
    paths: tuple[Path, ...]
    marker: str | None

    def collects(self, suite: Suite) -> bool:
        """Would this command collect that file, on paths alone?"""
        return any(
            target == suite.path or (target.is_dir() and target in suite.path.parents)
            for target in self.paths
        )


def _resolve(token: str) -> list[Path]:
    """A pytest target as the paths it names. Globs expand; non-paths vanish.

    `vektra-*/tests` is a glob the shell expands, and a runner is free to use one —
    it is the cure for the hand-maintained list, so the guard has to understand it.
    A token that matches nothing in the repo is not a path (a stray flag value, say),
    and is ignored rather than mistaken for coverage.
    """
    return sorted(REPO_ROOT.glob(token.rstrip("/")))


def _invocations(script: str, source: str) -> list[Invocation]:
    found = []
    for line in script.replace("\\\n", " ").splitlines():
        line = line.strip()
        # A `#` line is a comment even inside a `run:` block, and a comment runs nothing.
        if line.startswith("#") or "pytest" not in line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if "pytest" not in tokens:
            continue

        paths: list[Path] = []
        marker: str | None = None
        rest = tokens[tokens.index("pytest") + 1 :]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "-m":
                marker = rest[i + 1] if i + 1 < len(rest) else None
                i += 2
            elif token.startswith("-"):
                i += 1
            else:
                paths.extend(_resolve(token))
                i += 1

        if paths:
            found.append(Invocation(source, tuple(paths), marker))
    return found


def _makefile_invocations() -> list[Invocation]:
    recipe: list[str] = []
    inside = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("test:"):
            inside = True
        elif inside:
            if line.startswith("\t"):
                recipe.append(line)
            elif line.strip():
                break  # the next target begins
    return _invocations("\n".join(recipe), "the `test` target in the Makefile")


def _workflow_invocations() -> list[Invocation]:
    found = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job_id, job in (data.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if run := step.get("run"):
                    found.extend(
                        _invocations(run, f"job `{job_id}` in {workflow.name}")
                    )
    return found


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------


def test_the_runners_are_discovered() -> None:
    """A parser that silently found nothing would make every check below vacuous."""
    assert len(_suites()) >= 40, "test files are not being discovered"
    assert _makefile_invocations(), "no pytest command found in the `make test` recipe"
    assert len(_workflow_invocations()) >= 8, "workflow pytest commands not discovered"


def test_every_marker_expression_is_understood() -> None:
    """A `-m` expression the guard cannot reason about must not be waved through."""
    unknown = {
        f"{inv.source}: -m {inv.marker!r}"
        for inv in _makefile_invocations() + _workflow_invocations()
        if inv.marker not in _SELECTS_UNIT
    }
    assert unknown == set(), (
        "this guard decides coverage from the -m filter, and it does not know what "
        "these expressions select. Teach it, or it will certify suites nobody runs:\n  "
        + "\n  ".join(sorted(unknown))
    )


def test_the_deliberately_unrun_suites_still_exist() -> None:
    """A stale exemption is a suite quietly re-covered, or a file long gone."""
    stale = [rel for rel in EXPECTED_UNRUN if not (REPO_ROOT / rel).is_file()]
    assert stale == [], (
        "EXPECTED_UNRUN exempts files that no longer exist; drop them:\n  "
        + "\n  ".join(stale)
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_every_unit_suite_runs_in_make_test() -> None:
    """The local gate and CI are two hand-written lists; neither may lose a suite."""
    runs = _makefile_invocations()
    missing = [
        s.rel
        for s in _suites()
        if s.has_unit
        and s.rel not in EXPECTED_UNRUN
        and not any(i.collects(s) and _SELECTS_UNIT[i.marker] for i in runs)
    ]
    assert missing == [], (
        "`make test` does not run these unit tests, so the local gate is blind to "
        "them and has drifted from CI:\n  " + "\n  ".join(missing)
    )


def test_every_unit_suite_runs_in_ci() -> None:
    runs = _workflow_invocations()
    missing = [
        s.rel
        for s in _suites()
        if s.has_unit
        and s.rel not in EXPECTED_UNRUN
        and not any(i.collects(s) and _SELECTS_UNIT[i.marker] for i in runs)
    ]
    assert missing == [], (
        "no CI job runs these unit tests. Nothing will go red when they break:\n  "
        + "\n  ".join(missing)
    )


def test_every_integration_suite_runs_in_ci() -> None:
    """The DEBT-030 case: marked `integration`, excluded from every unit run by
    `-m "not integration"`, and named by no workflow. Belonging to a directory that
    both runners list is not enough, and is precisely what made this invisible."""
    runs = _workflow_invocations()
    missing = [
        s.rel
        for s in _suites()
        if s.has_integration
        and s.rel not in EXPECTED_UNRUN
        and not any(i.collects(s) and _SELECTS_INTEGRATION[i.marker] for i in runs)
    ]
    assert missing == [], (
        "these tests are marked `integration`, which every unit run excludes, and no "
        "workflow runs them with `-m integration`. They are executed by nothing:\n  "
        + "\n  ".join(missing)
    )
