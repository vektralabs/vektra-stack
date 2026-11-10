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
    import re

    return {
        m.group(1).strip(): m.group(2)
        for m in re.finditer(r"location\s+([^{]+)\{([^}]*)\}", block)
    }


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
        "^~ /api/v1/assess/transcripts/",
        "/",
    }
    assert (
        ui_locations["/"].strip() == "return 404;"
    )  # no mint, teacher or admin route here
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
    installed, so they import ``vektra_shared`` only (checks that need ``vektra_assess`` live
    in the assess tests)."""
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
                if top.startswith("vektra_") and top != "vektra_shared":
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
    # the guard must have something to judge: the sink and the canary datasets exist in the tree
    assert any(a.endswith("fixtures/mailsink.py") for a in assets), (
        "mail sink not found"
    )
    assert any("/canary/" in a for a in assets), "canary datasets not found"
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
