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

import itertools
import posixpath
import re
import shlex
import subprocess
from pathlib import Path

import pytest

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
    "secrets/assess/assess_token_signing": (
        "an assess keyring written by `make assess-keys`: the HS256 key that signs every "
        "discussion and download token, so a copy forges any student's session"
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


COMPOSE_ASSESS = REPO_ROOT / "docker-compose.assess.yml"
_SECRET_FILE = re.compile(r"\$\{VEKTRA_ASSESS_SECRETS_PATH:-[^}]*\}/([A-Za-z0-9_.-]+)")


def test_assess_keyrings_under_an_overridden_secrets_path_cannot_reach_the_image() -> (
    None
):
    """``VEKTRA_ASSESS_SECRETS_PATH`` can point anywhere, including a copied directory
    (``./migrations/private``): every keyring file name the compose overlay mounts is
    excluded wherever it sits, not only under a directory called ``secrets``."""
    names = sorted(
        set(_SECRET_FILE.findall(COMPOSE_ASSESS.read_text(encoding="utf-8")))
    )
    assert len(names) >= 10, f"only {names} parsed out of {COMPOSE_ASSESS.name}"
    ignore = DockerIgnore(DOCKERIGNORE.read_text(encoding="utf-8"))
    directories = copied_directories(DOCKERFILE.read_text(encoding="utf-8"))
    leaks = [
        candidate
        for base in directories
        for name in names
        for candidate in (f"{base}/private/{name}", f"{base}/a/b/{name}")
        if not ignore.excludes(candidate)
    ]
    assert leaks == [], f"keyring paths not excluded by .dockerignore: {leaks}"
    # the control: without the per-name rules, only a `secrets/` directory is covered
    weaker = DockerIgnore("**/secrets/\n")
    assert not weaker.excludes(f"migrations/private/{names[0]}")
    # the rules are exact names: the assess source modules still ship
    assert not ignore.excludes("vektra-app/src/vektra_app/assess_worker.py")


# --- plan 9: the assess discussion UI bundle -----------------------------------------------


def test_the_assess_ui_bundle_is_built_in_its_own_stage_and_only_its_output_ships() -> (
    None
):
    """The node stage builds vektra-assess/ui; the builder takes only the built static dir
    (hashed assets + manifest) into the package before the non-editable install; the UI
    sources and node_modules are never copied into the builder or runtime stages, and the
    build emits no source map."""
    text = DOCKERFILE.read_text()
    assert "FROM node:22-slim AS assess-ui-builder" in text
    copies = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("COPY")]
    stage_ui = [c for c in copies if "--from=assess-ui-builder" in c]
    assert stage_ui == [
        "COPY --from=assess-ui-builder /assess/src/vektra_assess/ui/static "
        "vektra-assess/src/vektra_assess/ui/static"
    ]
    # outside the node stage, nothing of vektra-assess/ui is copied
    builder_part = text.split("AS builder", 1)[1]
    assert "vektra-assess/ui" not in builder_part.replace("vektra_assess/ui/static", "")
    ignore = DOCKERIGNORE.read_text()
    assert "**/node_modules" in ignore
    assert "vektra-assess/src/vektra_assess/ui/static" in ignore
    esbuild = (REPO_ROOT / "vektra-assess/ui/esbuild.config.mjs").read_text()
    assert "sourcemap: false" in esbuild and "manifest.json" in esbuild
    pyproject = (REPO_ROOT / "vektra-assess/pyproject.toml").read_text()
    assert 'artifacts = ["src/vektra_assess/ui/static/*"]' in pyproject


def _nginx_servers(conf: str) -> dict[tuple[str, str], str]:
    """``(server_name, listen) -> block text`` of the example's server blocks."""
    import re

    out: dict[tuple[str, str], str] = {}
    for block in conf.split("\nserver {")[1:]:
        name = re.search(r"server_name\s+([^;]+);", block)
        listen = re.search(r"listen\s+([^;]+);", block)
        assert name and listen
        out[(name.group(1).strip(), listen.group(1).split()[0])] = block
    return out


def _locations(block: str) -> dict[str, str]:
    """``location modifier+path -> body``, braces balanced: a body can hold a nested block
    (``limit_except GET { deny all; }``), so the first ``}`` is not the end of the location."""
    import re

    out: dict[str, str] = {}
    for m in re.finditer(r"location\s+([^{]+)\{", block):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(block[i], 0)
            i += 1
        out[m.group(1).strip()] = block[m.end() : i - 1]
    return out


def test_the_nginx_example_serves_the_assess_ui_on_its_own_origin() -> None:
    """Plan 9 review #6 (ARCH-076): the discussion UI has its own server block (its own origin,
    the one in VEKTRA_ASSESS_UI_ORIGIN): the page, its assets and only the student API routes the
    page calls on its own origin (CSP connect-src 'self'), never X-Frame-Options; everything else
    there is 404. The API server keeps X-Frame-Options DENY and does not serve the page."""
    conf = (REPO_ROOT / "deploy/nginx/vektra.conf.example").read_text()
    servers = _nginx_servers(conf)
    api, ui = servers[("YOUR_DOMAIN", "443")], servers[("YOUR_UI_DOMAIN", "443")]

    api_server_level = api.split("location ", 1)[0]
    assert "add_header X-Frame-Options DENY always;" in api_server_level
    api_locations = _locations(api)
    assert "return 404;" in api_locations["^~ /api/v1/assess/ui"]
    assert "proxy_buffering off;" in api_locations["/"]

    assert "X-Frame-Options" not in ui
    ui_server_level = ui.split("location ", 1)[0]
    assert (
        'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
        in ui_server_level
    )
    assert "add_header X-Content-Type-Options nosniff always;" in ui_server_level
    ui_locations = _locations(ui)
    for body in ui_locations.values():
        assert (
            "add_header" not in body
        )  # nginx would drop the server-level headers there
    assert set(ui_locations) == {
        "^~ /api/v1/assess/ui",
        "= /api/v1/assess/state",
        "^~ /api/v1/assess/sessions",
        "~ ^/api/v1/assess/transcripts/[^/]+$",
        "/",
    }
    assert (
        ui_locations["/"].strip() == "return 404;"
    )  # no mint, teacher or admin route here
    # the transcript download only: the download-token mint and the resend are POSTs under
    # the same prefix and stay on the API origin (CodeRabbit 4531676327)
    transcripts = ui_locations["~ ^/api/v1/assess/transcripts/[^/]+$"]
    assert "limit_except GET" in transcripts and "deny all;" in transcripts
    assert (
        "proxy_buffering off;" in ui_locations["^~ /api/v1/assess/sessions"]
    )  # the events
    for name, body in ui_locations.items():
        if name != "/":
            # the client's Host with its port: the page compares scheme+Host with
            # VEKTRA_ASSESS_UI_ORIGIN, and $host drops a non-default port (CodeRabbit review
            # 5811279241, nginx example)
            assert "proxy_set_header Host $http_host;" in body
            assert "proxy_set_header X-Forwarded-Proto $scheme;" in body


def test_the_shared_tests_import_no_other_vektra_package() -> None:
    """CodeRabbit 4501067261: the shared foundation's tests run without the other packages
    installed, so they import ``vektra_shared`` and the dev-only ``vektra_testkit`` only (checks
    that need ``vektra_assess`` live in the assess tests; the kit itself depends on
    ``vektra_shared`` alone, plan task 3.1)."""
    import ast

    offenders = []
    for path in sorted((REPO_ROOT / "vektra-shared" / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                top = name.split(".")[0]
                if top.startswith("vektra_") and top not in {
                    "vektra_shared",
                    "vektra_testkit",
                }:
                    offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == []


# --- plan assess-hardening-release: test assets never ship ----------------------------------
# Test-only material that must stay out of the published image: the SMTP mail sink, the canary
# datasets and planted canary strings, the fixtures, and the planning artefacts. Paths, not disk:
# a directory the Dockerfile copies is checked against each class of test asset.
TEST_ASSET_SEGMENTS = ("tests", "fixtures", ".s2s", "canary")


def _shipped_roots() -> list[str]:
    return copied_directories(DOCKERFILE.read_text(encoding="utf-8"))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _tracked_files() -> list[str]:
    """Every file git tracks, repo-relative (never ``node_modules`` or build output)."""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def shipped_test_assets(roots: list[str], files: list[str]) -> list[str]:
    """The files under a copied directory that have a test-asset segment ANYWHERE in their path
    (``migrations/fixtures/...`` included), not only those under a top-level tests tree."""
    return sorted(
        f
        for f in files
        if any(_under(f, r) for r in roots)
        and any(seg in TEST_ASSET_SEGMENTS for seg in f.split("/")[:-1])
    )


CANARY = re.compile(r"CNRY-[a-z_]+-")
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".html",
    ".css",
    ".jinja",
    ".j2",
    ".txt",
    ".json",
    ".sql",
}


def canary_offenders(roots: list[str], files: dict[str, str]) -> list[str]:
    """Files under a copied directory whose text carries a planted canary string."""
    return sorted(
        f
        for f, text in files.items()
        if any(_under(f, r) for r in roots) and CANARY.search(text)
    )


def test_no_copied_directory_is_a_test_or_planning_tree() -> None:
    offenders = [
        d
        for d in _shipped_roots()
        if any(seg in TEST_ASSET_SEGMENTS for seg in d.strip("/").split("/"))
    ]
    assert offenders == [], f"the Dockerfile copies test or planning trees: {offenders}"


def test_tracked_test_assets_fall_outside_every_copied_directory() -> None:
    """The mail sink, the canary datasets and every fixture live under a ``tests/`` tree; none of
    those files may sit under a directory the Dockerfile copies."""
    roots = _shipped_roots()
    assets = [
        p.relative_to(REPO_ROOT).as_posix()
        for pattern in ("vektra-*/tests/**/*", "tests/**/*")
        for p in REPO_ROOT.glob(pattern)
        if p.is_file() and "__pycache__" not in p.parts
    ]
    # the guard must have something to judge: the sink and the parser corpus exist in the tree
    # (the canary datasets and the recordings left with the Moodle provider, plan 2b)
    assert any(a.endswith("fixtures/mailsink.py") for a in assets), (
        "mail sink not found"
    )
    assert any("/fixtures/parser/" in a for a in assets), "parser corpus not found"
    shipped = sorted(a for a in assets if any(_under(a, r) for r in roots))
    assert shipped == [], f"test assets inside a copied directory: {shipped[:20]}"


def test_planning_artefacts_are_excluded_from_the_build_context() -> None:
    ignore = DockerIgnore(DOCKERIGNORE.read_text(encoding="utf-8"))
    assert ignore.excludes(".s2s/plans/INDEX-ASSESS.md")
    assert ignore.excludes(".s2s/requirements.md")


def test_the_mail_sink_library_is_a_dev_dependency_only() -> None:
    """``aiosmtpd`` (the test relay) is a dev dependency of the workspace root, never a runtime
    dependency of a shipped package, and no shipped source imports it."""
    import tomllib

    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert any(d.startswith("aiosmtpd") for d in root["dependency-groups"]["dev"])
    for pyproject in sorted(REPO_ROOT.glob("vektra-*/pyproject.toml")):
        deps = (
            tomllib.loads(pyproject.read_text())
            .get("project", {})
            .get("dependencies", [])
        )
        assert not any(d.startswith("aiosmtpd") for d in deps), pyproject
    importers = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.glob("vektra-*/src/**/*.py")
        if re.search(
            r"^\s*(import|from)\s+aiosmtpd", p.read_text(errors="replace"), re.M
        )
    ]
    assert importers == []


def test_no_planted_canary_string_is_in_shipped_source() -> None:
    """Canary strings (``CNRY-<field>-...``) belong to the test datasets; one in shipped source
    would make every leak test meaningless (the canary would be in the image's own bytes). Every
    copied tree is scanned, the migrations included, not only ``vektra-*/src``."""
    roots = _shipped_roots()
    texts = {
        f: (REPO_ROOT / f).read_text(errors="replace")
        for f in _tracked_files()
        if any(_under(f, r) for r in roots) and Path(f).suffix in TEXT_SUFFIXES
    }
    assert any(f.startswith("migrations/") for f in texts), "migrations not scanned"
    assert canary_offenders(roots, texts) == []


def test_the_test_asset_guard_can_say_no() -> None:
    """Negative control: a Dockerfile that copies a tests tree is caught."""
    bad = "FROM python:3.12\nCOPY vektra-assess/tests vektra-assess/tests\n"
    dirs = copied_directories(bad)
    assert any(
        any(seg in TEST_ASSET_SEGMENTS for seg in d.strip("/").split("/")) for d in dirs
    ), dirs


def test_a_nested_test_asset_inside_a_copied_directory_is_caught() -> None:
    """Negative control (CodeRabbit nitpick on #195): a fixture nested in a copied tree, such
    as ``migrations/fixtures/``, is a test asset in the image and must be reported."""
    roots = ["migrations", "vektra-assess/src"]
    files = [
        "migrations/versions/0001_initial.py",
        "migrations/fixtures/seed.json",
        "vektra-assess/src/vektra_assess/tests/helper.py",
        "vektra-assess/tests/test_x.py",  # outside every copied root: not shipped
    ]
    assert shipped_test_assets(roots, files) == [
        "migrations/fixtures/seed.json",
        "vektra-assess/src/vektra_assess/tests/helper.py",
    ]


def test_no_tracked_test_asset_sits_anywhere_under_a_copied_directory() -> None:
    files = _tracked_files()
    assert any(f.startswith("migrations/") for f in files), "tracked files not listed"
    assert shipped_test_assets(_shipped_roots(), files) == []


def test_a_canary_string_in_shipped_migrations_is_caught() -> None:
    """Negative control: the canary scan covers every copied tree, migrations included."""
    files = {
        "migrations/versions/0099_x.py": "SEED = 'CNRY-snapshot-1'",
        "vektra-assess/src/vektra_assess/a.py": "x = 1",
        "vektra-assess/tests/t.py": "CNRY-snapshot-2",  # tests are not shipped
    }
    assert canary_offenders(["migrations", "vektra-assess/src"], files) == [
        "migrations/versions/0099_x.py"
    ]


# --- plan task 3.1: the test kit never ships -------------------------------------------------
# The recording doubles and the LMS conformance kit live in ``vektra-testkit`` (``testkit/``), a
# workspace member installed by the dev dependency groups only. A workspace member is installed
# by a plain ``uv sync`` even with ``--no-dev``, so the Dockerfile skips it by name: these checks
# replay the Dockerfile's own ``uv sync`` flags against the lock, build the ``vektra_shared``
# wheel, and each has a negative control proving it can say no.
TESTKIT = "vektra-testkit"
TESTKIT_MODULES = ("testing_recorders", "vektra_testkit")
_UV_SYNC = re.compile(r"\buv sync\b[^;&|]*")


def _uv() -> str:
    """The uv binary: ``uv run`` exports it as ``UV``; a missing uv fails, it never skips."""
    import os
    import shutil

    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv, "uv not found: the test kit boundary cannot be checked"
    return uv


def dockerfile_uv_syncs(dockerfile_text: str) -> list[list[str]]:
    """The argument list of every ``uv sync`` the Dockerfile runs (both branches of an if)."""
    return [
        shlex.split(m.group(0))[2:]
        for line in _logical_lines(dockerfile_text)
        if line.split(maxsplit=1)[0].upper() == "RUN"
        for m in _UV_SYNC.finditer(line)
    ]


def exported_packages(sync_args: list[str]) -> set[str]:
    """The distributions a ``uv sync`` with these flags installs, read from the lock by
    ``uv export`` (same resolution, nothing installed)."""
    import subprocess

    out = subprocess.run(
        [_uv(), "export", "--no-hashes", "--no-annotate", "--no-header", *sync_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("./"):  # a workspace member, by path
            path = line.split()[0]
            names.add(_member_name(REPO_ROOT / path / "pyproject.toml"))
            continue
        names.add(re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].lower())
    return names


def _member_name(pyproject: Path) -> str:
    import tomllib

    return str(tomllib.loads(pyproject.read_text())["project"]["name"])


def kit_offenders(names: list[str]) -> list[str]:
    """The archive entries that are test kit modules."""
    return sorted(
        n for n in names if any(m in n.replace("\\", "/") for m in TESTKIT_MODULES)
    )


def _build_wheel(package: str, out_dir: Path) -> list[str]:
    import subprocess
    import zipfile

    subprocess.run(
        [_uv(), "build", "--wheel", "--package", package, "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    (wheel,) = out_dir.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def test_the_test_kit_is_a_dev_dependency_only() -> None:
    """``vektra-testkit`` is in dev groups only: no shipped package depends on it at runtime,
    no shipped source imports it, and ``testing_recorders`` is gone from ``vektra_shared``."""
    import tomllib

    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert "testkit" in root["tool"]["uv"]["workspace"]["members"]
    assert TESTKIT in root["dependency-groups"]["dev"]
    for pyproject in sorted(REPO_ROOT.glob("vektra-*/pyproject.toml")):
        project = tomllib.loads(pyproject.read_text()).get("project", {})
        runtime = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            runtime.extend(extra)
        assert not any(d.startswith(TESTKIT) for d in runtime), pyproject
    assert not (
        REPO_ROOT / "vektra-shared/src/vektra_shared/testing_recorders.py"
    ).exists()
    importers = [
        f"{p.relative_to(REPO_ROOT).as_posix()}: {name}"
        for p in sorted(REPO_ROOT.glob("vektra-*/src/**/*.py"))
        for name in kit_imports(p.read_text(errors="replace"))
    ]
    assert importers == []


def kit_imports(source: str) -> list[str]:
    """The test-kit modules a source imports, read from its AST: every alias of every
    ``import`` (``import os, vektra_testkit.recorders`` included) and every ``from`` import,
    by module and by imported name (``from vektra_shared import testing_recorders``, and the
    relative ``from . import testing_recorders``)."""
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] if module else []
            names += [f"{module}.{a.name}" if module else a.name for a in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "vektra_testkit" or "testing_recorders" in parts:
                found.append(name)
    return found


def test_the_kit_import_check_can_say_no() -> None:
    """Negative control (CodeRabbit 4544866406): every import form of the kit is caught,
    the multi-import line included, and an unrelated import is not."""
    source = "\n".join(
        [
            "import os, vektra_testkit.recorders",
            "import json as j, vektra_testkit",
            "from vektra_testkit.lms_conformance import kit",
            "from vektra_shared import testing_recorders",
            "from vektra_shared.testing_recorders import RecordingLLMProvider",
            "from . import testing_recorders",
            "import vektra_shared.registry",
            "from vektra_shared.types import Message",
        ]
    )
    assert kit_imports(source) == [
        "vektra_testkit.recorders",
        "vektra_testkit",
        "vektra_testkit.lms_conformance",
        "vektra_testkit.lms_conformance.kit",
        "vektra_shared.testing_recorders",
        "vektra_shared.testing_recorders",
        "vektra_shared.testing_recorders.RecordingLLMProvider",
        "testing_recorders",
    ]


def test_the_dockerfile_never_copies_the_test_kit_source() -> None:
    sources = [_normalise(s) for s in context_copy_sources(DOCKERFILE.read_text())]
    assert "testkit/pyproject.toml" in sources  # uv needs the member's manifest
    assert [
        s for s in sources if _under(s, "testkit") and s != "testkit/pyproject.toml"
    ] == []


def test_every_image_sync_skips_the_test_kit() -> None:
    syncs = dockerfile_uv_syncs(DOCKERFILE.read_text())
    assert len(syncs) >= 3, syncs  # dependencies layer + both branches of the install
    for args in syncs:
        assert "--no-dev" in args, args
        pairs = list(itertools.pairwise(args))
        assert ("--no-install-package", TESTKIT) in pairs, args


def test_the_image_resolution_has_no_test_kit_and_no_pytest() -> None:
    """Replays the default build's final ``uv sync`` flags against ``uv.lock``."""
    install = default_install()
    names = exported_packages(install)
    assert "vektra-shared" in names and "vektra-app" in names, sorted(names)
    assert TESTKIT not in names
    assert "pytest" not in names


def test_the_image_resolution_check_can_say_no() -> None:
    """Negative control: the same sync without the skip installs the kit, so the check above
    is not vacuous (a plain ``--no-dev`` sync installs every workspace member)."""
    install = default_install()
    i = install.index("--no-install-package")
    assert TESTKIT in exported_packages(install[:i] + install[i + 2 :])


def test_the_vektra_shared_wheel_has_no_test_kit(tmp_path: Path) -> None:
    names = _build_wheel("vektra-shared", tmp_path)
    assert "vektra_shared/registry.py" in names, names[:20]
    assert kit_offenders(names) == []


def test_the_wheel_check_can_say_no(tmp_path: Path) -> None:
    """Negative control: the test kit's own wheel is caught by the same check."""
    names = _build_wheel(TESTKIT, tmp_path)
    assert "vektra_testkit/recorders.py" in kit_offenders(names)


# --- ADR-0033 D6, OD-6 (plan tasks 2a-4b.10, 2b.2, 2b.3): a provider ships only in its variant ---
# The default image holds no provider: no ``vektra_lms_*`` package. The variant built with
# ``--build-arg INSTALL_LMS_PROVIDER=moodle`` holds the Moodle one, and since phase 2b that is the
# wheel released from the provider's repository, installed by version and sha256 from its pin file
# ``docker/lms-providers/<name>.txt`` (the one place the pin lives), never built from this tree.
# The build picks a provider stage by that argument (``FROM lms-provider-${INSTALL_LMS_PROVIDER:-none}``),
# and BuildKit builds only the stage it picks, so the checks below read the Dockerfile per stage.
# The built image is checked by tests/assess/test_lms_provider_image.py (the assess-stack job).
LMS_ARG = "INSTALL_LMS_PROVIDER"
LMS_DIST_PREFIX = "vektra-lms-"
PIN_DIR = "docker/lms-providers"
_FROM = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_PIN_LINE = re.compile(
    r"^(?P<dist>vektra-lms-[a-z0-9-]+) @ "
    r"(?P<url>https://github\.com/vektralabs/[a-z0-9-]+/releases/download/(?P<tag>[^/\s]+)/"
    r"(?P<file>[^/\s]+\.whl)) --hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)
#: the flags every provider install must carry, and why
REQUIRED_INSTALL_FLAGS = (
    "--require-hashes",  # the downloaded bytes must have the pinned sha256, or the build fails
    "--no-deps",  # vektra-shared is on no index: the venv's own is used, checked by `uv pip check`
    "--no-index",  # nothing is looked up anywhere: the pinned URL is the only source
    "--no-config",  # the workspace's [tool.uv] settings are not this install's
)


def dockerfile_stages(dockerfile_text: str) -> list[tuple[str, str, list[str]]]:
    """(name, base, logical lines) of every stage, in order; lines before the first FROM are
    the global section, named ``""``."""
    stages: list[tuple[str, str, list[str]]] = [("", "", [])]
    for line in _logical_lines(dockerfile_text):
        m = _FROM.match(line)
        if m:
            stages.append((m.group(2) or "", m.group(1), []))
        else:
            stages[-1][2].append(line)
    return stages


def _stage(name: str) -> tuple[str, str, list[str]]:
    (found,) = [s for s in dockerfile_stages(DOCKERFILE.read_text()) if s[0] == name]
    return found


def selected_provider_stage(dockerfile_text: str, value: str) -> str:
    """The stage the ``venv`` stage is built FROM with ``INSTALL_LMS_PROVIDER=value``."""
    (base,) = [b for n, b, _ in dockerfile_stages(dockerfile_text) if n == "venv"]
    m = re.fullmatch(r"(.*)\$\{" + LMS_ARG + r":-([^}]*)\}(.*)", base)
    assert m, f"the venv stage is not chosen by {LMS_ARG}: FROM {base}"
    return m.group(1) + (value or m.group(2)) + m.group(3)


def _stage_syncs(name: str) -> list[list[str]]:
    return dockerfile_uv_syncs("\n".join(_stage(name)[2]))


def default_install() -> list[str]:
    """The last ``uv sync`` of the default build (the builder stage's install)."""
    return _stage_syncs("builder")[-1]


def parse_pin(text: str) -> dict[str, str]:
    """The one requirement of a pin file: a ``vektra-lms-*`` wheel by https URL from a vektralabs
    release, with its sha256; the version is the wheel file name's, and the release tag must
    carry it. Anything else (no hash, a second line, an index name, a version the URL does not
    carry) is refused."""
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise ValueError(
            f"a pin file holds exactly one requirement, found {len(lines)}"
        )
    m = _PIN_LINE.match(lines[0])
    if not m:
        raise ValueError(f"not a hashed wheel URL of a vektralabs release: {lines[0]}")
    pin = m.groupdict()
    package = pin["dist"].replace("-", "_")
    w = re.fullmatch(re.escape(package) + r"-([^-]+)-py3-none-any\.whl", pin["file"])
    if not w:
        raise ValueError(
            f"the wheel {pin['file']} is not a pure wheel of {pin['dist']}"
        )
    pin["version"] = w.group(1)
    if not pin["tag"].endswith(f"v{pin['version']}"):
        raise ValueError(
            f"release tag {pin['tag']} does not carry version {pin['version']}"
        )
    return pin


def provider_install_problems(name: str, lines: list[str]) -> list[str]:
    """What is wrong with the provider stage ``lms-provider-<name>``: it must copy its pin file
    and nothing else from the context, and run one ``uv pip install`` of exactly that file with
    every flag of REQUIRED_INSTALL_FLAGS, then ``uv pip check``; no ``uv sync`` and no other
    install (a sync or a second install could add a provider that no hash pins)."""
    problems = []
    pin = f"{PIN_DIR}/{name}.txt"
    copies = [
        shlex.split(ln)[1:]
        for ln in lines
        if ln.split(maxsplit=1)[0].upper() in {"COPY", "ADD"}
    ]
    sources = [_normalise(s) for s in context_copy_sources("\n".join(lines))]
    if sources != [pin]:
        problems.append(f"copies {sources} from the context, not exactly {pin}")
    dest = copies[0][-1] if len(copies) == 1 else None
    runs = [ln for ln in lines if ln.split(maxsplit=1)[0].upper() == "RUN"]
    if len(runs) != 1:
        problems.append(f"{len(runs)} RUN instructions, not one")
    installs = [
        m.group(0)
        for ln in runs
        for m in re.finditer(r"\b(?:uv )?pip3? install\b[^;&|]*", ln)
    ]
    if len(installs) != 1:
        problems.append(f"{len(installs)} installs, not one: {installs}")
    else:
        args = shlex.split(installs[0])
        for flag in REQUIRED_INSTALL_FLAGS:
            if flag not in args:
                problems.append(f"the install lacks {flag}")
        reqs = [b for a, b in itertools.pairwise(args) if a in {"-r", "--requirement"}]
        positional = [
            a
            for prev, a in itertools.pairwise(["", *args])
            if not a.startswith("-")
            and prev not in {"-r", "--requirement", "--python", "-p"}
            and a not in {"uv", "pip", "pip3", "install"}
        ]
        if reqs != [dest] or positional:
            problems.append(
                f"installs {reqs + positional}, not only the pin file {dest}"
            )
    if not any(re.search(r"\buv pip check\b", ln) for ln in runs):
        problems.append("no `uv pip check` after the install")
    if any(_UV_SYNC.search(ln) for ln in runs):
        problems.append("a provider stage must not run uv sync")
    return problems


def _provider_stages(text: str) -> dict[str, list[str]]:
    return {
        n.removeprefix("lms-provider-"): lines
        for n, _b, lines in dockerfile_stages(text)
        if n.startswith("lms-provider-") and n != "lms-provider-none"
    }


def test_the_default_build_selects_the_stage_without_a_provider() -> None:
    text = DOCKERFILE.read_text()
    global_lines = dockerfile_stages(text)[0][2]
    assert f"ARG {LMS_ARG}=" in global_lines, "the argument must default to empty"
    assert selected_provider_stage(text, "") == "lms-provider-none"
    assert selected_provider_stage(text, "moodle") == "lms-provider-moodle"
    names = [n for n, _b, _l in dockerfile_stages(text)]
    assert {"lms-provider-none", "lms-provider-moodle", "venv"} <= set(names)
    _n, base, lines = _stage("lms-provider-none")
    assert base == "builder" and lines == [], "the no-provider stage adds nothing"
    # the runtime takes its venv from the selected stage only
    runtime = _stage("runtime")[2]
    venv_copies = [ln for ln in runtime if "/app/.venv" in ln and ln.startswith("COPY")]
    assert venv_copies == ["COPY --from=venv /app/.venv /app/.venv"], venv_copies


def test_every_provider_stage_installs_its_hashed_pin_and_nothing_else() -> None:
    """Plan 2b.2: each provider stage installs the one wheel its pin file names, by sha256, and
    each pin file has its stage (a pin no build reads would be a pin nobody checks)."""
    stages = _provider_stages(DOCKERFILE.read_text())
    pins = sorted(p.stem for p in (REPO_ROOT / PIN_DIR).glob("*.txt"))
    assert "moodle" in stages, sorted(stages)
    assert sorted(stages) == pins, (sorted(stages), pins)
    for name, lines in stages.items():
        assert provider_install_problems(name, lines) == [], name


def test_the_moodle_pin_is_one_hashed_wheel_of_its_release() -> None:
    """Plan 2b.3: the variant's wheel is pinned by version and sha256 in one place, and that
    place names the provider's own release (the provider repository's ``provider-v*`` tag)."""
    pin = parse_pin((REPO_ROOT / PIN_DIR / "moodle.txt").read_text(encoding="utf-8"))
    assert pin["dist"] == "vektra-lms-moodle"
    assert pin["url"].startswith(
        "https://github.com/vektralabs/vektra-moodle-discuss/releases/download/provider-v"
    ), pin["url"]
    assert pin["tag"] == f"provider-v{pin['version']}"
    assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])


def test_the_pin_parser_can_say_no() -> None:
    """Negative control: every way a pin can stop pinning is refused."""
    url = (
        "https://github.com/vektralabs/vektra-moodle-discuss/releases/download/"
        "provider-v0.9.0/vektra_lms_moodle-0.9.0-py3-none-any.whl"
    )
    sha = "--hash=sha256:" + "a" * 64
    good = f"# comment\nvektra-lms-moodle @ {url} {sha}\n"
    assert parse_pin(good)["version"] == "0.9.0"
    bad = {
        "no hash": f"vektra-lms-moodle @ {url}",
        "md5": f"vektra-lms-moodle @ {url} --hash=md5:" + "a" * 32,
        "two lines": good + good,
        "by name": f"vektra-lms-moodle==0.9.0 {sha}",
        "plain http": f"vektra-lms-moodle @ {url.replace('https', 'http')} {sha}",
        "another host": f"vektra-lms-moodle @ https://example.org/vektra_lms_moodle-0.9.0-py3-none-any.whl {sha}",
        "tag without the version": f"vektra-lms-moodle @ {url.replace('provider-v0.9.0', 'latest')} {sha}",
        "another package's wheel": f"vektra-lms-moodle @ {url.replace('vektra_lms_moodle-', 'evil-')} {sha}",
        "an sdist": f"vektra-lms-moodle @ {url.replace('-py3-none-any.whl', '.tar.gz')} {sha}",
    }
    for case, text in bad.items():
        with pytest.raises(ValueError):
            parse_pin(text)
            pytest.fail(case)


def test_the_provider_stage_check_can_say_no() -> None:
    """Negative control: a stage that builds the provider from a tree, drops the hash check,
    installs a second requirement or resolves dependencies is reported."""
    pin = f"{PIN_DIR}/moodle.txt"
    good = [
        f"COPY {pin} /tmp/p.txt",
        "RUN uv pip install --no-config --python /app/.venv/bin/python --no-index --no-cache "
        "--require-hashes --no-deps -r /tmp/p.txt && uv pip check --no-config --python /app/.venv/bin/python",
    ]
    assert provider_install_problems("moodle", good) == []
    cases = {
        "from a tree": [
            "COPY adapters/vektra-lms-moodle/src adapters/vektra-lms-moodle/src",
            "RUN uv sync --frozen --no-dev --group lms-moodle",
        ],
        "no hash check": [good[0], good[1].replace("--require-hashes ", "")],
        "resolves deps": [good[0], good[1].replace("--no-deps ", "")],
        "a second package": [
            good[0],
            good[1].replace("-r /tmp/p.txt", "-r /tmp/p.txt evil"),
        ],
        "no pip check": [good[0], good[1].split(" && ")[0]],
        "another file": [*good, "COPY docker/entrypoint.sh /x"],
        "two installs": [*good, "RUN pip install vektra-lms-moodle==0.9.0"],
    }
    for case, lines in cases.items():
        assert provider_install_problems("moodle", lines), case


def _fake_wheel(out: Path) -> Path:
    """A minimal, valid pure wheel (``vektra_lms_fake``) to install offline."""
    import base64
    import hashlib
    import zipfile

    files = {
        "vektra_lms_fake/__init__.py": b"X = 1\n",
        "vektra_lms_fake-0.0.1.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: vektra-lms-fake\nVersion: 0.0.1\n"
        ),
        "vektra_lms_fake-0.0.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record.append(f"{name},sha256={digest.decode()},{len(data)}")
    record.append("vektra_lms_fake-0.0.1.dist-info/RECORD,,")
    wheel = out / "vektra_lms_fake-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("vektra_lms_fake-0.0.1.dist-info/RECORD", "\n".join(record) + "\n")
    return wheel


def _replayed_install(pin_file: Path, python: Path) -> subprocess.CompletedProcess[str]:
    """The moodle stage's own ``uv pip install`` (its flags, read from the Dockerfile), with only
    the interpreter and the pin file swapped for local ones."""
    import subprocess

    (run,) = [ln for ln in _stage("lms-provider-moodle")[2] if ln.startswith("RUN")]
    m = re.search(r"\buv pip install\b[^;&|]*", run)
    assert m, run
    args = shlex.split(m.group(0))[3:]
    swapped = []
    for prev, arg in itertools.pairwise(["", *args]):
        if prev == "--python":
            arg = str(python)
        elif prev in {"-r", "--requirement"}:
            arg = str(pin_file)
        swapped.append(arg)
    return subprocess.run(
        [_uv(), "pip", "install", *swapped],
        cwd=REPO_ROOT,  # the workspace config is next to it: --no-config must keep it out
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_provider_install_refuses_a_wheel_with_another_hash(tmp_path: Path) -> None:
    """Plan 2b.2, failing first: the stage's real install command, run offline on a local wheel,
    installs it when the pin carries its sha256 and fails, installing nothing, when one hex digit
    differs. That failure is the build failure a tampered or re-uploaded release produces."""
    import hashlib
    import subprocess

    wheel = _fake_wheel(tmp_path)
    sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wrong = ("0" if sha[0] != "0" else "1") + sha[1:]
    subprocess.run(
        [_uv(), "venv", "--no-config", "-q", str(tmp_path / "venv")],
        check=True,
        cwd=tmp_path,
    )
    python = tmp_path / "venv/bin/python"
    pin = tmp_path / "pin.txt"

    pin.write_text(f"vektra-lms-fake @ {wheel.as_uri()} --hash=sha256:{wrong}\n")
    refused = _replayed_install(pin, python)
    assert refused.returncode != 0, refused.stdout
    assert "hash" in refused.stderr.lower(), refused.stderr
    assert not list((tmp_path / "venv").rglob("vektra_lms_fake/__init__.py"))

    pin.write_text(f"vektra-lms-fake @ {wheel.as_uri()} --hash=sha256:{sha}\n")
    installed = _replayed_install(pin, python)
    assert installed.returncode == 0, installed.stderr
    assert list((tmp_path / "venv").rglob("vektra_lms_fake/__init__.py"))


def test_the_default_resolution_has_no_lms_provider() -> None:
    """Replays the default install against ``uv.lock``: no ``vektra-lms-*`` distribution."""
    names = exported_packages(default_install())
    assert "vektra-assess" in names, sorted(names)
    assert [n for n in names if n.startswith(LMS_DIST_PREFIX)] == []


def test_the_repository_resolves_and_holds_no_provider() -> None:
    """Plan 2b acceptance: ``adapters/`` is gone, no tracked file is a provider's source, and
    neither the root's dependency groups nor the lock name a ``vektra-lms-*`` distribution (the
    variant's provider comes from its pin file, never from the workspace resolution)."""
    import tomllib

    assert not (REPO_ROOT / "adapters").exists()
    assert [
        f for f in _tracked_files() if re.search(r"(^|/)vektra_lms_[a-z0-9_]+/", f)
    ] == []
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    grouped = [
        d
        for group in root.get("dependency-groups", {}).values()
        for d in group
        if isinstance(d, str) and d.startswith(LMS_DIST_PREFIX)
    ]
    assert grouped == []
    assert not [
        s
        for s in root["tool"]["uv"].get("sources", {})
        if s.startswith(LMS_DIST_PREFIX)
    ]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    assert [
        p["name"] for p in lock["package"] if p["name"].startswith(LMS_DIST_PREFIX)
    ] == []


def test_no_shipped_source_imports_an_lms_provider() -> None:
    """The default image's packages never import a provider (import-linter says the same for
    the import graph; this reads every shipped source, templates and strings included)."""
    pattern = re.compile(r"\b(import|from)\s+vektra_lms_\w*")
    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted(REPO_ROOT.glob("vektra-*/src/**/*.py"))
        if pattern.search(p.read_text(errors="replace"))
    ]
    assert offenders == []
