"""The platform must not name a specific LMS product.

Vektra describes itself as general RAG infrastructure with an *LMS-agnostic* e-learning
vertical: `.s2s/CONTEXT.md` has listed the Moodle adapter as a separate repository,
`vektra-moodle`, since it was written (2026-01-28), and has said "vektra-learn is
LMS-agnostic" since the next day. That boundary
held for as long as it was only ever exercised by people who knew it: across the whole
history of `vektra-learn/src`, no commit has ever touched the string "moodle".

It broke as soon as it was not. The rule lived only in prose, nothing measured it, and
the assessment work brought Moodle into the platform both inside its own package and in
`vektra-shared`, `vektra-app` and `vektra-admin`. Nothing went red, because nothing could.

This guard makes the boundary measurable. A platform source file that names an LMS product
fails, with the file and the line, unless it is recorded in `lms_boundary_baseline.toml`.

**The baseline is the static-analysis pattern of the same name** (PHPStan
`phpstan-baseline.neon`, detekt `baseline.xml`, Psalm `psalm-baseline.xml`): known
violations recorded in a data file that may only shrink. Two kinds of entry:

- a **file** entry carries the number of mentions it had when recorded. More mentions, or a
  new file, is red. *Fewer* mentions is red too, until the entry is lowered or removed: an
  entry left above the truth would silently re-admit what was just taken out, which turns
  a debt counter into a list of excuses;
- a **directory** entry exempts a whole package while it is being separated, without a
  count (it is under active development, and a count would block every commit on it). It
  goes red when the directory no longer names any LMS: then it must be deleted.

Removing the last entry is the definition of "the separation is done".

**Perimeter.** What the platform ships: every tracked file under a `src/` directory of a
workspace member (read from the root `pyproject.toml`, not listed here), which includes
`vektra-learn/widget/src` and data such as prompt strings in YAML, plus the Alembic
migrations (their location read from `alembic.ini`), which are the platform's schema and
are copied into the image. Not tests: an adapter's integration test may legitimately use
Moodle as data. Not `docs/` or the CHANGELOG, where naming the adapter repositories is the
point. Not vendored minified assets (`*.min.css`, `*.min.js`): third-party code such as
Pico CSS uses `canvas` as the HTML element.

The perimeter is read from `git ls-files`, so an untracked scratch file cannot make the
guard pass or fail, and a missing git is a failure, never a silent pass.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("lms_boundary_baseline.toml")

# LMS products, from Wikipedia's "List of learning management systems" (consulted
# 2026-11-08) plus Schoology and Totara, which it omits. Only distinctive tokens: a name
# that is also an ordinary word or an HTML element ("canvas", "blackboard", "classroom")
# is matched in its product form only, or the guard would fail on Pico CSS on day one.
# To add a product: append its name, lower case; separators are normalised (below).
LMS_NAMES: tuple[str, ...] = (
    "moodle",
    "totara",
    "sakai",
    "d2l",
    "brightspace",
    "chamilo",
    "openedx",
    "open edx",
    "ilias",
    "claroline",
    "atutor",
    "openolat",
    "itslearning",
    "schoology",
    "edmodo",
    "docebo",
    "webct",
    "lon-capa",
    "canvas lms",
    "instructure",
    "blackboard learn",
    "google classroom",
)

SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".html",
    ".jinja",
    ".j2",
    ".css",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".md",
}
_VENDORED = re.compile(r"\.min\.(css|js)$")

# Each line is normalised before matching, so every way code spells a name is the same
# string: CamelCase is split, including after an acronym (`MoodleClient`, `VektraMoodle`,
# `LMSMoodle`, `D2LClient`), then everything that is not a letter or digit becomes one
# space (`canvas_lms`, `CANVAS-LMS`, `open_edx`). A multi-word name is also matched written
# together (`canvaslms`). Names of six letters or more are matched anywhere, so a compound
# like `getmoodleclient` is caught; shorter ones need a left boundary ("ilias" is inside
# "familias"), and the shortest also a right one ("d2l").
_CAMEL = re.compile(r"(?<=[A-Z0-9])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise(text: str) -> str:
    return _NOT_ALNUM.sub(" ", _CAMEL.sub(" ", text).lower())


def _pattern(form: str) -> re.Pattern[str]:
    if len(form.replace(" ", "")) >= 6:
        return re.compile(re.escape(form))
    right = r"(?![a-z0-9])" if len(form) < 5 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(form) + right)


_FORMS: list[tuple[str, re.Pattern[str]]] = []
for _name in LMS_NAMES:
    _spaced = _normalise(_name).strip()
    for _form in dict.fromkeys((_spaced, _spaced.replace(" ", ""))):
        _FORMS.append((_name, _pattern(_form)))


@dataclass(frozen=True)
class Mention:
    path: str
    line: int
    name: str


@dataclass(frozen=True)
class Baseline:
    files: dict[str, int]
    directories: tuple[str, ...]


# ---------------------------------------------------------------------------
# Pure functions: everything below them is tested on synthetic trees first
# ---------------------------------------------------------------------------
def mentions_in(path: str, text: str) -> list[Mention]:
    found: list[Mention] = []
    for number, line in enumerate(text.splitlines(), start=1):
        folded = _normalise(line)
        for name, pattern in _FORMS:
            found.extend(Mention(path, number, name) for _ in pattern.finditer(folded))
    return found


def in_perimeter(path: str, members: list[str], migrations: str = "migrations") -> bool:
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).suffix not in SOURCE_SUFFIXES or _VENDORED.search(path):
        return False
    if path.startswith(migrations.rstrip("/") + "/"):
        return PurePosixPath(path).suffix == ".py"
    return (
        len(parts) > 1
        and parts[0] in members
        and "src" in parts
        and "tests" not in parts
    )


def _covered_by_directory(path: str, directories: tuple[str, ...]) -> str | None:
    return next((d for d in directories if path.startswith(d.rstrip("/") + "/")), None)


def violations(
    tree: dict[str, str],
    members: list[str],
    baseline: Baseline,
    migrations: str = "migrations",
) -> list[str]:
    """Everything wrong with `tree` (path -> text) against `baseline`, as messages."""
    problems: list[str] = []
    per_file: dict[str, list[Mention]] = {}
    per_directory: dict[str, int] = {d: 0 for d in baseline.directories}
    for path, text in tree.items():
        if not in_perimeter(path, members, migrations):
            continue
        found = mentions_in(path, text)
        if not found:
            continue
        directory = _covered_by_directory(path, baseline.directories)
        if directory is not None:
            per_directory[directory] += len(found)
        else:
            per_file[path] = found

    for path, found in sorted(per_file.items()):
        allowed = baseline.files.get(path, 0)
        if len(found) > allowed:
            where = ", ".join(f"line {m.line} ({m.name})" for m in found)
            problems.append(
                f"{path} names an LMS product {len(found)} time(s), baseline allows "
                f"{allowed}: {where}"
            )
    for path, allowed in sorted(baseline.files.items()):
        actual = len(per_file.get(path, []))
        if actual < allowed:
            problems.append(
                f"{path}: baseline allows {allowed} mention(s) but {actual} remain. Lower "
                f"or remove the entry in {BASELINE.name}, or the removed ones can return "
                f"unnoticed"
            )
    for directory, count in sorted(per_directory.items()):
        if count == 0:
            problems.append(
                f"{directory}: exempt in {BASELINE.name} but names no LMS any more. "
                f"Delete the entry: that is the separation being done"
            )
    return problems


def load_baseline(path: Path = BASELINE) -> Baseline:
    data = tomllib.loads(path.read_text())
    files: dict[str, int] = {}
    directories: list[str] = []
    for entry in data.get("entry", []):
        assert entry.get("reason", "").strip(), (
            f"baseline entry without a reason: {entry}"
        )
        assert entry.get("since", "").strip(), f"baseline entry without a date: {entry}"
        if "directory" in entry:
            directories.append(entry["directory"])
        else:
            files[entry["file"]] = int(entry["mentions"])
    return Baseline(files=files, directories=tuple(directories))


def workspace_members() -> list[str]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return list(root["tool"]["uv"]["workspace"]["members"])


def migrations_dir() -> str:
    """The Alembic script location, from `alembic.ini` rather than a literal."""
    parser = (
        configparser.RawConfigParser()
    )  # the value uses %(here)s, Alembic's own key
    parser.read(REPO_ROOT / "alembic.ini")
    location = parser["alembic"]["script_location"].strip().rstrip("/")
    return location.removeprefix("%(here)s/")


def tracked_tree(members: list[str], migrations: str) -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *members, migrations],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return {
        p: (REPO_ROOT / p).read_text(errors="replace")
        for p in out.split("\0")
        if p and in_perimeter(p, members, migrations)
    }


# ---------------------------------------------------------------------------
# The guard has to be able to say no: synthetic trees
# ---------------------------------------------------------------------------
MEMBERS = ["vektra-core", "vektra-learn", "vektra-assess"]
EMPTY = Baseline(files={}, directories=())


def test_a_planted_mention_in_a_platform_package_is_red_with_file_and_line() -> None:
    tree = {"vektra-core/src/vektra_core/x.py": "a = 1\nclient = MoodleClient()\n"}
    found = violations(tree, MEMBERS, EMPTY)
    assert len(found) == 1
    assert (
        "vektra-core/src/vektra_core/x.py" in found[0] and "line 2 (moodle)" in found[0]
    )


def test_an_exempt_directory_stays_green_with_the_same_mention() -> None:
    tree = {"vektra-assess/src/vektra_assess/x.py": "client = MoodleClient()\n"}
    exempt = Baseline(files={}, directories=("vektra-assess/src",))
    assert violations(tree, MEMBERS, exempt) == []


def test_an_empty_baseline_and_one_mention_is_red() -> None:
    tree = {"vektra-learn/widget/src/chat.js": "// sent by the moodle block\n"}
    assert len(violations(tree, MEMBERS, EMPTY)) == 1


def test_legitimate_platform_vocabulary_is_not_matched() -> None:
    """A matcher that excluded everything would pass for free: these must all count zero."""
    lines = [
        "from vektra_learn import api  # the LMS-agnostic vertical",
        "const ctx = document.createElement('canvas').getContext('2d');",
        "canvas { display: block }",
        "the blackboard example; learning management system; an LTI launch",
        "familias = 'ilia'; d2lx = 1",  # boundaries: inside a word, or a longer token
    ]
    assert mentions_in("vektra-core/src/x.py", "\n".join(lines)) == []


def test_every_spelling_of_a_product_is_matched() -> None:
    text = (
        "MoodleClient\nmoodle_ws\nVektraMoodle\nmoodlews\nMOODLE_URL\nCanvas LMS\n"
        "D2L\nsakaimodule\nCANVAS_LMS\ncanvas-lms\ncanvaslms\nopen_edx\nLON_CAPA\n"
        "LMSMoodle\nD2LClient\ngetmoodleclient\n"
    )
    assert [m.name for m in mentions_in("vektra-core/src/x.py", text)] == [
        "moodle",
        "moodle",
        "moodle",
        "moodle",
        "moodle",
        "canvas lms",
        "d2l",
        "sakai",
        "canvas lms",
        "canvas lms",
        "canvas lms",
        "open edx",
        "lon-capa",
        "moodle",
        "d2l",
        "moodle",
    ]


def test_the_perimeter_excludes_only_tests_vendored_assets_and_non_members() -> None:
    assert in_perimeter("vektra-core/src/vektra_core/x.py", MEMBERS)
    assert in_perimeter("vektra-learn/widget/src/chat-ui.js", MEMBERS)
    assert in_perimeter("vektra-core/src/vektra_core/static/site.css", MEMBERS)
    assert in_perimeter("vektra-core/src/vektra_core/strings/en.yaml", MEMBERS)
    assert in_perimeter("migrations/versions/0042_x.py", MEMBERS)
    assert not in_perimeter("migrations/README", MEMBERS)
    assert not in_perimeter("vektra-core/tests/test_x.py", MEMBERS)
    assert not in_perimeter("vektra-core/src/vektra_core/static/pico.min.css", MEMBERS)
    assert not in_perimeter("docs/reference/api.md", MEMBERS)
    assert not in_perimeter("vektra-moodle/src/x.py", MEMBERS)  # not a member


def test_a_file_entry_ratchets_in_both_directions() -> None:
    path = "vektra-core/src/vektra_core/x.py"
    two = {path: "moodle\nmoodle\n"}
    assert violations(two, MEMBERS, Baseline(files={path: 2}, directories=())) == []
    assert len(violations(two, MEMBERS, Baseline(files={path: 1}, directories=()))) == 1
    stale = violations(two, MEMBERS, Baseline(files={path: 3}, directories=()))
    assert len(stale) == 1 and "Lower or remove" in stale[0]


def test_an_exempt_directory_that_names_no_lms_must_be_deleted() -> None:
    tree = {"vektra-assess/src/vektra_assess/x.py": "nothing here\n"}
    found = violations(
        tree, MEMBERS, Baseline(files={}, directories=("vektra-assess/src",))
    )
    assert len(found) == 1 and "Delete the entry" in found[0]


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------
def test_the_perimeter_reads_the_real_platform_sources() -> None:
    """A perimeter that found nothing would make the real check vacuous."""
    migrations = migrations_dir()
    tree = tracked_tree(workspace_members(), migrations)
    assert "vektra-learn/src/vektra_learn/api.py" in tree
    assert any(p.startswith(migrations + "/versions/") for p in tree)
    assert "vektra-learn/widget/src/chat-ui.js" in tree
    assert not any("/tests/" in p for p in tree)
    assert not any(_VENDORED.search(p) for p in tree)


def test_the_platform_names_no_lms_beyond_the_baseline() -> None:
    members, migrations = workspace_members(), migrations_dir()
    tree = tracked_tree(members, migrations)
    problems = violations(tree, members, load_baseline(), migrations)
    assert not problems, "\n".join(problems)
