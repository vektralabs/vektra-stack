# Contributing to Vektra RAG

## Quick start

```bash
# Clone and install dev environment
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all workspace dependencies
uv sync --dev

# Install pre-commit hooks
uv tool install pre-commit --with pre-commit-uv --force-reinstall
uv run pre-commit install

# Run all unit tests
uv run pytest vektra-shared/tests/ vektra-core/tests/ vektra-ingest/tests/ \
              vektra-index/tests/ vektra-admin/tests/ vektra-analytics/tests/ \
              vektra-learn/tests/ -v -m "not integration"
```

## Development tools

All tools are version-pinned in `pyproject.toml` and `uv.lock`. No separate installation needed
after `uv sync --dev`.

| Tool | Purpose | Run |
|------|---------|-----|
| **Ruff** | Linting + formatting (replaces flake8, isort, black) | `uv run ruff check .` / `uv run ruff format .` |
| **mypy** | Type checking with SQLAlchemy 2.0 plugin | `uv run mypy vektra_shared vektra_core ...` |
| **import-linter** | Enforce module boundary contracts | `uv run lint-imports` |
| **pytest** | Test runner | `uv run pytest <path>` |
| **pytest-cov** | Coverage reporting | `uv run pytest --cov` |
| **pre-commit** | Run all checks before each commit | automatic on `git commit` |

All tool configuration lives in the root `pyproject.toml` under `[tool.*]` sections.
There are no separate `.flake8`, `.mypy.ini`, or `.importlinter` files.

## Branch strategy

We use a simplified GitFlow:

```text
main ────────────────────────────────────────────── (production-ready, protected)
  │                                                 ↑
  └─── develop ───────────────────────────────────── (integration, protected)
           │                                         ↑
           ├─── feature/core-streaming ──────────────┘
           ├─── fix/ingest-conflict-409 ─────────────┘
           └─── docs/update-contributing ─────────────┘
```

- All work happens on `feature/*`, `fix/*`, or `docs/*` branches off `develop`.
- Merge to `develop` via PR (requires CI to pass).
- Merge `develop → main` for releases (requires CI + review).
- Direct pushes to `main` and `develop` are not permitted.

### Branch naming

```text
feat/<component>-<short-description>    # new feature
fix/<component>-<short-description>     # bug fix
docs/<short-description>                # documentation only
chore/<short-description>               # tooling, dependencies, CI
```

## Commit message format

We use [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>

[optional body]

[optional footer]
Signed-off-by: Your Name <your.email@example.com>
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

**Scopes**: `core`, `ingest`, `index`, `admin`, `shared`, `ci`, `docs`

**Examples**:
```text
feat(ingest): add PDF text extraction with pdfplumber
fix(core): handle empty conversation history in streaming
docs(readme): update installation instructions
chore(ci): add path filtering to unit test workflow
```

All commits must be signed off with a DCO (`git commit -s`). This certifies you have the
right to submit the code under the project's license.

## Commit and tag signing

Branch protection on `develop` and `main` requires **cryptographically signed commits**.
GitHub will block all merge types (merge, squash, rebase) if any source commit is unsigned.

Release tags are signed too. That is a separate setting: **`commit.gpgsign` does not
cover tags**, so with it alone `git tag -a` produces an annotated tag with no signature,
and nothing warns you. Branch protection does not catch it either, because it gates
commits, not tags. `v0.7.1` was cut that way and is the one recent tag without a
signature.

**Set up before your first commit** (recovering unsigned commits is painful):

```bash
# 1. Configure git to sign with your SSH key
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/<your-key>.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true    # separate from commit.gpgsign; without it, tags are unsigned

# 2. Register the key on GitHub as BOTH Authentication AND Signing key
#    Settings > SSH and GPG keys > New SSH key (select type for each)

# 3. (Optional) Enable local signature verification
echo "$(git config user.email) $(cat ~/.ssh/<your-key>.pub)" > ~/.ssh/allowed_signers
git config --global gpg.ssh.allowedSignersFile ~/.ssh/allowed_signers

# 4. Test
git commit --allow-empty -m "chore: verify signing setup"
git log --show-signature -1   # should show "Good ssh signature"

# 5. Test tags too: the settings are independent, so this can fail while step 4 passes
git tag -a -m probe _probe
git verify-tag _probe; signed=$?
git tag -d _probe   # unconditionally: a leftover _probe makes the next run die on
                    # "tag already exists", hiding the "no signature found" you came for
[ $signed -eq 0 ] && echo "tags are signed" || echo "tags are NOT signed: set tag.gpgsign"
```

**Remote servers without browser**: use `gh auth login --with-token` and add the
`admin:ssh_signing_key` scope upfront to avoid needing browser-based re-auth later.

## Changelog

We follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). The
file must always have an `[Unreleased]` section at the top, even if empty.

**During development** — every PR with user-visible impact adds an entry under
`[Unreleased]` in `CHANGELOG.md`, using one of the six standard sections:

- **Added** — new features
- **Changed** — changes in existing functionality
- **Deprecated** — features that will be removed in upcoming releases
- **Removed** — features removed in this release
- **Fixed** — bug fixes
- **Security** — vulnerability fixes

Internal-only changes (refactors with no behavior change, CI tweaks, test-only
edits) do not require a changelog entry.

**At release time** — in the release-prep PR (`chore/vX.Y.Z-release`):

1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
2. Add a fresh empty `## [Unreleased]` block above the new release entry
3. Bump the version in all `vektra-*/pyproject.toml` from the previous release to
   `X.Y.Z` (there is no `-dev` suffix between releases), and in every `__version__`
   constant, which nothing derives from `pyproject.toml`: `git grep -n '^__version__ ='`
   lists them
4. Refresh `uv.lock` to match the new versions
5. Bump the published-image examples (`docs/getting-started/index.md`,
   `deploy/docker-compose.image.yml.example`) to the tag this release publishes

**Then cut the tag**, on `main` after the release PR is merged. This step used to live
only in people's heads, which is how `v0.7.1` ended up unsigned:

```bash
git tag -s vX.Y.Z -m "vX.Y.Z - <one-line theme>"   # -s, not -a: see "Commit and tag signing"
git verify-tag vX.Y.Z                              # refuse to push if this says "no signature found"
git push origin vX.Y.Z
```

Pushing the tag is what triggers `publish.yml` to build and push
`ghcr.io/vektralabs/vektra:{version}` and `:{version}-ocr`. **A tag is therefore not
cheap to redo**: moving it re-triggers the build and the images come back with
different digests, so verify the signature *before* pushing rather than after.

Finally, create the GitHub Release. Nothing does this automatically: `release.yml` is a
disabled placeholder, and the Releases page silently fell three versions behind before
anyone noticed. The notes are the CHANGELOG section for the version, so extract it to a
file rather than retyping it:

```bash
# everything between this version's heading and the next one
awk '/^## \[X\.Y\.Z\]/{f=1; next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/notes-X.Y.Z.md

gh release create vX.Y.Z \
  --title "vX.Y.Z - <one-line theme>" \
  --notes-file /tmp/notes-X.Y.Z.md \
  --verify-tag        # fails instead of creating a tag if vX.Y.Z is not pushed yet
```

## PR workflow

1. Create a branch: `git checkout -b feat/core-streaming`
2. Make changes and write tests
3. Run checks locally: `uv run pre-commit run --all-files`
4. Open PR targeting `develop`
5. Wait for CI (lint + unit tests) and AI reviews (CodeRabbit, Gemini)
6. Address review comments
7. PR is merged when CI passes and at least one reviewer approves

### Cross-component changes

If your change affects multiple components (e.g., a new shared protocol interface), open a
single PR that covers all affected packages. Document the cross-component impact in the PR
description.

## PR checklist

Before opening a PR:

- [ ] `uv run pre-commit run --all-files` passes locally (ruff, mypy, import-linter)
- [ ] Unit tests pass: `uv run pytest <component>/tests/ -m "not integration"`
- [ ] New behavior has test coverage
- [ ] Commit messages follow Conventional Commits with `-s` sign-off
- [ ] `CHANGELOG.md` has an `[Unreleased]` entry for any user-visible change
- [ ] PR description explains the change and motivation

## CI pipeline

Every PR runs two workflows automatically:

| Workflow | Trigger | Checks |
|----------|---------|--------|
| **Lint** | Every push and PR | ruff lint, ruff format, mypy, import-linter |
| **Unit tests** | Every PR (path-filtered) | pytest unit tests for changed components |

The `ci-gate` job aggregates all unit test results and is the single required status check
for branch protection. A job being skipped (component not changed) does not block the gate.

An **integration workflow** (using Docker Compose + real PostgreSQL) runs on PRs to `develop`
and `main`. It enforces hard performance gates:
- NFR-002: search p95 < 500ms
- NFR-004: container startup < 60s
- NFR-007: 100% audit log completeness
- NFR-009: 100% error code actionability

Two AI reviewers comment on PRs automatically:
- **CodeRabbit** (line-by-line, 40+ linters, component-specific instructions)
- **Gemini Code Assist** (wide context, 1M token window)

## Module boundaries

The eight components are strictly isolated. Cross-component imports are enforced by
import-linter and will fail CI:

```text
vektra_shared     <-  everything may import this
vektra_core       <-  no imports from admin/ingest/index/analytics/learn
vektra_ingest     <-  no imports from admin/core/index/analytics/learn
vektra_index      <-  no imports from admin/core/ingest/analytics/learn
vektra_admin      <-  no imports from core/ingest/index/analytics/learn
vektra_analytics  <-  no imports from core/ingest/index/admin/learn
vektra_learn      <-  no imports from core/ingest/index/admin/analytics
vektra_app        <-  may import all (assembly layer)
```

If a component needs functionality from another, it should go through a Protocol interface
in `vektra_shared`.

## The Moodle LMS provider

The assessment vertical reads its LMS only through the `LmsProvider` Protocol
(`vektra_shared.lms`, ADR-0033). The Moodle implementation, the distribution `vektra-lms-moodle`
(package `vektra_lms_moodle`), is **not in this repository**: it lives in
[vektra-moodle-discuss](https://github.com/vektralabs/vektra-moodle-discuss) under `provider/`,
with its tests, its 4.3 and 5.2 recordings, the canary matrix and the joint suites. That
repository's CI runs them against the vektra-stack commit pinned in `provider/vektra-stack.ref`,
including the weekly live contract run against the dev Moodle sites, and releases the wheel
(`provider-v<version>` tags, with its sha256).

**What this repository keeps.** The image variant `lms-moodle` (`INSTALL_LMS_PROVIDER=moodle`,
what `docker-compose.assess.yml` builds) installs that released wheel, pinned by version, URL and
sha256 in [`docker/lms-providers/moodle.txt`](docker/lms-providers/moodle.txt), the one place the
pin lives. The checks here are about the platform and the image, not the provider's behaviour:
`vektra-shared/tests/test_image_content_boundary.py` (the stage installs that pin with
`--require-hashes`, a wrong hash fails the install, the default image resolves no provider) and,
in the `assess-stack` CI job, `tests/assess/test_lms_provider_image.py` (the built variant holds
exactly the pinned wheel's files, and the platform's loader builds the provider from it) plus a
worker that starts on it. And one that is about the provider: `test-lms-provider` (`ci-unit.yml`)
runs the provider's own suites, at the release tag of the pinned wheel, against the PR's tree, so
a platform change that breaks the provider fails the PR that makes it.

**Working on the provider** (in its repository; this workspace supplies `vektra-shared` and the
vertical's test harness, so the provider is installed into this workspace's venv):

```bash
git clone git@github.com:vektralabs/vektra-moodle-discuss.git ../vektra-moodle-discuss
uv sync --dev --frozen
uv pip install --no-deps -e ../vektra-moodle-discuss/provider
export VEKTRA_STACK_ROOT="$PWD"    # the joint suites import vektra-assess/tests from here
cd ../vektra-moodle-discuss
UV_NO_SYNC=1 "$VEKTRA_STACK_ROOT/.venv/bin/python" -m pytest provider/tests
```

`--no-deps`: the provider requires `vektra-shared`, which is on no index; this workspace's is
used. `UV_NO_SYNC=1`, and no `uv run`/`uv sync` here while you work on it: the provider is not in
`uv.lock`, so a sync removes it from the venv (the joint suites' fixtures call `uv run alembic`).
Run `uv sync` again to return to the plain workspace.

**Bumping the pin** after a provider release: replace the one requirement line of
`docker/lms-providers/moodle.txt` (version, URL and sha256 change together; check the hash of
the downloaded wheel yourself), build the variant (`docker compose -f docker-compose.yml
-f docker-compose.assess.yml build vektra`; the build fails if the wheel's hash differs or a
requirement of the wheel is not met by the image), and run
`VEKTRA_ASSESS_STACK=1 uv run pytest tests/assess/test_lms_provider_image.py` against it. Note the
bump in the CHANGELOG.

## Documentation standards

Follow [Diataxis](https://diataxis.fr/): identify whether your doc is a tutorial, how-to
guide, reference, or explanation before writing.

- Cross-cutting docs: `docs/`
- Component-specific: component's `README.md`
- Architecture decisions: `.s2s/decisions/ADR-*.md`

## Getting help

- **Questions**: Open a GitHub Discussion
- **Bugs**: Open a GitHub Issue with reproduction steps
- **Ideas**: Open a GitHub Discussion in the Ideas category

## Code of Conduct

By participating in this project you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

*Last updated: 2026-03-21. Contribution guidelines derived from roundtable session
20260128-roundtable-vektra (REQ-016, REQ-011) and CI/CD setup discussion 20260219.*
