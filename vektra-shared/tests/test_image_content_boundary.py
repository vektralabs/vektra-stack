"""Nothing sensitive may reach the published container image (INFRA-008).

`ghcr.io/vektralabs/vektra` is a **public** package. Every `v*` tag pushes two
images automatically (`publish.yml`), so whatever the build context lets through is
world-readable the moment the tag lands, with no human between the push and the
public. Package visibility on GitHub is also one-way: it cannot be made private
again.

Two things keep secrets out today, and both are conventions that a single careless
edit removes without anything going red:

1. the Dockerfile never copies the build context root, only named paths;
2. `.dockerignore` excludes the files that carry credentials and node-local detail.

This guard turns both into checks. It reasons about the *paths*, never about what
happens to exist on disk: CI checks out a clean tree where `.env` does not exist, so
a guard that scanned the working tree would pass for the wrong reason on exactly the
machine that publishes the image.

**Why the patterns must be `**`-prefixed.** A `.dockerignore` entry without a slash
matches at the context root only. Measured against a real `docker build` on
2026-09-02: with `.dockerignore` containing `.env`, the root `.env` was excluded
while `sub/.env` and `sub/deep/.env` were both copied into the image; with `**/.env`
all three were excluded and unrelated nested files still arrived. That matters here
because the Dockerfile copies whole directories (`vektra-learn/widget/`,
`migrations/`, every `vektra-*/src`), so a nested `.env` is a reachable path, not a
hypothetical one.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Files that must never enter the image, with what each one leaks. Checked at the
# root of every copied directory and one level deeper, because the Dockerfile copies
# directories, not single files.
FORBIDDEN: dict[str, str] = {
    ".env": (
        "the deployment's real configuration: Postgres password, admin bootstrap "
        "key, conversation encryption key, learn JWT secret, LLM API keys"
    ),
    ".env.local": "same as .env, developer-local override",
    ".env.production.local": "same as .env, environment-specific override",
    "CLAUDE.local.md": (
        "node-local agent instructions: internal hostnames, absolute home paths, "
        "private project names"
    ),
    ".claude/settings.local.json": "node-local agent configuration and permissions",
    ".local/notes.md": (
        "the gitignored scratch directory used across this crew's repos for local "
        "archives, exports and private denylists"
    ),
}

# Instructions whose source is a build stage or a pinned image, not the build
# context: they cannot smuggle a working-tree file in on their own, and whatever
# they carry came from a stage this guard already checked.
_FROM_FLAG = re.compile(r"^--from=", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------
def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations and drop comments, the way a builder does."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        joined.append(buffer.strip())
    return joined


def context_copy_sources(dockerfile_text: str) -> list[str]:
    """Every path a COPY/ADD reads **from the build context**.

    The destination (last argument) and `--from=` instructions are dropped; the JSON
    array form is handled, because `COPY ["a", "b"]` is the form a reviewer skims
    past.
    """
    sources: list[str] = []
    for line in _logical_lines(dockerfile_text):
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or parts[0].upper() not in {"COPY", "ADD"}:
            continue
        rest = parts[1].strip()
        args = shlex.split(rest.replace("[", " ").replace("]", " ").replace(",", " "))
        if any(_FROM_FLAG.match(a) for a in args):
            continue
        args = [a for a in args if not a.startswith("--")]
        if len(args) < 2:
            continue
        sources.extend(args[:-1])
    return sources


def _normalise(source: str) -> str:
    """The path Docker will actually resolve, not the string that was typed.

    A builder cleans a COPY source and drops leading slashes before resolving it
    against the context, so `././`, `./.` and `a/..` all name the context root.
    Comparing the literal string sees three different strings and waves all three
    through: reproduced against a real build, `COPY ././ /app` copies the entire
    context, gitignored files included.
    """
    return posixpath.normpath(source.strip().lstrip("/") or ".")


def sweeps_the_context_root(source: str) -> bool:
    """True when a source pulls in the whole context, dotfiles included."""
    normalised = _normalise(source)
    return normalised in {".", "", "*"} or normalised.startswith("..")


def copied_directories(dockerfile_text: str) -> list[str]:
    """The COPY sources that are directories, i.e. the ones that can *contain* a file.

    A source naming a single file (`alembic.ini`, `vektra-shared/pyproject.toml`)
    smuggles nothing on its own. A **wildcard** source is expanded rather than
    skipped: `COPY vektra-*/src ...` is a legitimate way to write this Dockerfile,
    and skipping it would silently drop eight directories from the check while the
    guard went on passing, which is the exact failure this file exists to prevent.
    `test_the_dockerfile_is_parsed` fails if this filter ever leaves nothing behind.
    """
    directories: list[str] = []
    for source in context_copy_sources(dockerfile_text):
        base = _normalise(source)
        if base in {".", ""} or base.startswith(".."):
            continue  # a context sweep; the check above fails the build for it
        if any(ch in base for ch in "*?["):
            directories.extend(
                str(match.relative_to(REPO_ROOT))
                for match in REPO_ROOT.glob(base)
                if match.is_dir()
            )
            continue
        if (REPO_ROOT / base).is_dir():
            directories.append(base)
    return sorted(set(directories))


# ---------------------------------------------------------------------------
# .dockerignore
# ---------------------------------------------------------------------------
class DockerIgnore:
    """The subset of `.dockerignore` matching this repository actually uses.

    Docker matches a pattern against the path relative to the context root and lets
    the **last** matching pattern decide, with a leading `!` re-including. A parent
    directory that matches excludes everything under it, which is what makes
    `.claude/` cover `.claude/settings.local.json`.
    """

    def __init__(self, text: str) -> None:
        self._rules: list[tuple[re.Pattern[str], bool]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:].strip()
            self._rules.append((self._compile(line.rstrip("/")), negated))

    @staticmethod
    def _compile(pattern: str) -> re.Pattern[str]:
        segments = pattern.strip("/").split("/")
        last = len(segments) - 1
        out: list[str] = []
        for index, segment in enumerate(segments):
            if segment == "**":
                # `**` consumes whole path segments including their separator, so it
                # must not get one of its own: `**/.env` has to match a bare `.env`.
                out.append(".*" if index == last else "(?:[^/]+/)*")
                continue
            out.append(
                "".join(
                    "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
                    for c in segment
                )
            )
            if index != last:
                out.append("/")
        return re.compile("^" + "".join(out) + "$")

    def excludes(self, path: str) -> bool:
        """Is `path` kept out of the build context?

        Every ancestor is tested too: excluding a directory excludes its contents.
        """
        verdict = False
        segments = path.strip("/").split("/")
        candidates = ["/".join(segments[: i + 1]) for i in range(len(segments))]
        for regex, negated in self._rules:
            if any(regex.match(candidate) for candidate in candidates):
                verdict = not negated
        return verdict


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------
def test_the_dockerfile_is_parsed() -> None:
    """A parser that found nothing would make every check below vacuous."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    sources = context_copy_sources(text)
    assert len(sources) >= 15, (
        f"only {len(sources)} context COPY sources parsed out of the Dockerfile; "
        "the parser is broken, not the Dockerfile"
    )
    directories = copied_directories(text)
    assert len(directories) >= 9, (
        f"only {len(directories)} of them resolve to real directories "
        f"({directories}); with none, the leak check below asserts nothing"
    )


def test_the_matcher_can_say_no() -> None:
    """A matcher that excluded everything would pass every check for free."""
    ignore = DockerIgnore(DOCKERIGNORE.read_text(encoding="utf-8"))
    must_ship = [
        "vektra-shared/src/vektra_shared/config.py",
        "migrations/env.py",
        "alembic.ini",
        "vektra-learn/widget/src/api-client.js",
    ]
    wrongly_excluded = [p for p in must_ship if ignore.excludes(p)]
    assert wrongly_excluded == [], (
        "the matcher excludes files the image needs, so it cannot be trusted when it "
        f"says a sensitive file is excluded: {wrongly_excluded}"
    )


def test_the_guard_detects_a_context_sweep() -> None:
    """The mutant this guard exists for: someone adds `COPY . .`."""
    mutant = "FROM python:3.12-slim\nCOPY . /app\n"
    assert any(sweeps_the_context_root(s) for s in context_copy_sources(mutant))
    other_forms = ["FROM x\nCOPY ./ /app\n", 'FROM x\nADD ["./", "/app"]\n']
    for form in other_forms:
        assert any(sweeps_the_context_root(s) for s in context_copy_sources(form)), (
            f"a context sweep written as {form!r} slipped past the guard"
        )


def test_the_guard_detects_a_sweep_written_as_an_equivalent_path() -> None:
    """`COPY ././ /app` is the context root; only a normalised comparison sees it."""
    for form in ("././", "./.", ".//", "a/..", "./*", "/", "*"):
        sources = context_copy_sources(f"FROM x\nCOPY {form} /app\n")
        assert any(sweeps_the_context_root(s) for s in sources), (
            f"{form!r} resolves to the build context root and was not flagged"
        )
    for legitimate in ("vektra-shared/src", "migrations/", "alembic.ini"):
        assert not sweeps_the_context_root(legitimate), (
            f"{legitimate!r} read as a context sweep; a guard that flags everything "
            "is as useless as one that flags nothing"
        )


def test_wildcard_directory_sources_are_expanded_not_skipped() -> None:
    """A glob source must widen the check, never silently narrow it."""
    expanded = copied_directories("FROM x\nCOPY vektra-*/src /app/src\n")
    assert len(expanded) >= 8, (
        f"a wildcard COPY source expanded to {expanded}: written that way, the "
        "Dockerfile would drop those directories from the check and the guard "
        "would still pass"
    )
    assert all(p.endswith("/src") for p in expanded), expanded


def test_the_guard_detects_a_lost_dockerignore_rule() -> None:
    """The other mutant: the `.env` rule is dropped, or loses its `**` prefix."""
    assert not DockerIgnore("# nothing here\n").excludes("migrations/.env")
    assert not DockerIgnore(".env\n").excludes("migrations/.env"), (
        "a root-only `.env` rule must NOT read as protecting a nested one: that is "
        "the exact gap measured against a real docker build"
    )
    assert DockerIgnore("**/.env\n").excludes("migrations/.env")


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def test_no_instruction_sweeps_the_build_context_root() -> None:
    offenders = [
        s
        for s in context_copy_sources(DOCKERFILE.read_text(encoding="utf-8"))
        if sweeps_the_context_root(s)
    ]
    assert offenders == [], (
        "the Dockerfile copies the whole build context "
        f"({offenders}), which puts every gitignored file in the working tree into a "
        "public image. Copy named paths instead."
    )


def test_no_forbidden_file_can_reach_the_image() -> None:
    ignore = DockerIgnore(DOCKERIGNORE.read_text(encoding="utf-8"))
    directories = copied_directories(DOCKERFILE.read_text(encoding="utf-8"))

    leaks: list[str] = []
    for base in directories:
        for name, what_it_leaks in FORBIDDEN.items():
            for candidate in (f"{base}/{name}", f"{base}/nested/{name}"):
                if not ignore.excludes(candidate):
                    leaks.append(f"{candidate} -> {what_it_leaks}")

    assert leaks == [], (
        "these paths are inside a directory the Dockerfile copies and are NOT "
        "excluded by .dockerignore, so they would be published in a public image:\n  "
        + "\n  ".join(sorted(set(leaks)))
        + "\n\nAdd a `**`-prefixed rule to .dockerignore: an entry without a slash "
        "matches the context root only."
    )
