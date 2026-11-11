# Release checklist: the release that ships vektra-assess

The general release procedure (changelog, version bump, signed tag, GitHub Release) is in
[CONTRIBUTING.md](../../../CONTRIBUTING.md#changelog). This page adds what a release that ships
the assessment discussion feature must check on top of it. It is a checklist, not a record:
cutting the release, and choosing its version, is the maintainer's decision.

State at the time of writing: on `develop` every workspace package is `0.8.0`, and `v0.8.0`
shipped from `main` without the assess feature (`vektra-assess` is not on `main`). The maintainer's plan is that the
assess work ships in the next minor, `0.9.0`. Nothing in this repository has been bumped for it.

## 1. Gates that must already be green

- [ ] **No unbound worker slot.** `vektra-app/tests/test_assess_processes.py` passes with
      `vektra_assess.worker.slots.PENDING_OWNER_PLANS` empty (it is, since the `rotate_keys`
      kind was removed in revision `0044`), so a production worker starts
      ([deployment](../operators/assess-deployment.md#worker-start-check)). A new job kind
      without a handler reopens this gate.
- [ ] **Chaos suite.** `make test-chaos-assess` exits 0 (its gate fails on a skipped case, on
      fewer cases or scenarios than declared, or on any invariant violation), on two consecutive
      nightly runs of `.github/workflows/chaos-assess.yml` (keep each run's `invariants.md`
      artifact as release evidence). GitHub starts `schedule` and `workflow_dispatch` runs only
      for a workflow file on the default branch, so this needs the workflow on `main` first. The
      tag run is checked in section 3.
- [ ] **Reference docs.** `vektra-assess/tests/test_reference_docs.py` passes (error codes,
      settings and routes match the code).
- [ ] **LMS boundary.** `vektra-shared/tests/test_lms_boundary.py` passes with no new baseline
      entry.
- [ ] **Image content boundary.** `vektra-shared/tests/test_image_content_boundary.py` passes:
      the assess UI assets are in the image; test fixtures, the mail sink, canary datasets and
      `.s2s/` are not.
- [ ] **Old-CPU import (BUG-027).** The "Image imports on a pre-x86-64-v2 CPU" step of
      `integration.yml` is green. Its import list already includes `vektra_assess`,
      `vektra_app.assess_worker`, `vektra_app.assess_parser`, the job loop and the parser stack;
      a new native dependency of assess must be added there.
- [ ] **Assess stack jobs.** The `assess-stack`, `assess-chaos` and `assess-ui-e2e` jobs of
      `integration.yml` are green. To rerun the browser job locally, `make test-assess-e2e` is not
      enough: it runs only `test_ui_e2e.py`, while the CI job also runs
      `test_transcript_inert_browser.py` and `test_transcript_a11y.py` (all three in
      `vektra-assess/tests/integration/`, `-m integration`).
- [ ] **LMS reader contract.** The latest run of `.github/workflows/assess-moodle-contract.yml`
      (manual and weekly; the web-service readers against real LMS 4.3 and 5.2 sites) is green.
      It does not test the plugin itself: the plugin release is tested in its own repository.

## 2. Release-prep PR (`chore/vX.Y.Z-release`)

Follow CONTRIBUTING.md, with these points for assess:

- [ ] Bump `version` in every `vektra-*/pyproject.toml`, `vektra-assess` included, and every
      `__version__` constant (`git grep -n '^__version__ ='`; `vektra_assess/__init__.py` has
      one), then `uv lock`.
- [ ] `CHANGELOG.md`: `[Unreleased]` becomes `[X.Y.0] - YYYY-MM-DD` with a fresh empty
      `[Unreleased]` above it. The assess entries under `[Unreleased]` stay in this release's
      section; check that none of them describes a behaviour that changed later in the cycle.
- [ ] Add an **upgrade** entry: the assess migrations run with the API's
      `alembic upgrade head`; the feature stays dormant unless `VEKTRA_ASSESS_ENABLED=true`, and
      the API applies the forbidden column of the key matrix even when it is off (an API
      container that mounts a worker-only secret no longer starts).
- [ ] Bump the published-image examples (`docs/getting-started/index.md`,
      `deploy/docker-compose.image.yml.example`).
- [ ] `VEKTRA_ASSESS_MIN_PLUGIN_VERSION` (default `0.1.0`, checked at mint against the plugin's
      `plugin_version`, `403 ERR-ASSESS-016` below it) equals the lowest plugin release that
      implements the contract this version serves. Raise the default in `config.py`,
      `configuration.md` and `.env.example` together if the contract changed.

## 3. Tag and image

- [ ] Signed tag on `main` after the release PR is merged (`git tag -s`, `git verify-tag`, then
      push): CONTRIBUTING.md.
- [ ] `publish.yml` built and pushed `ghcr.io/vektralabs/vektra:X.Y.Z` and `:X.Y.Z-ocr`.
- [ ] The `chaos-assess.yml` run triggered by the tag passed (0 invariant violations; keep its
      `invariants.md` artifact as release evidence).

## 4. Smoke test of the published image

On a scratch host, with the published image (`deploy/docker-compose.image.yml.example`) and the
assess overlay, a generated key set (`make assess-keys`) and a test relay:

- [ ] `GET /health` answers.
- [ ] `GET /health/assess` answers `200` `healthy` once the worker is up (`503` with
      `no_heartbeat` before).
- [ ] `python -m vektra_assess.keys verify --role worker` and `--role api` exit 0 (on an empty
      database every check is vacuous; this proves the keys load and the command runs).
- [ ] A setup check against a test LMS site reaches `done`, with every item `OK`
      ([walkthrough](../operators/assess-deployment.md#setup-check-walkthrough)).
- [ ] `POST /api/v1/assess/ops/smtp-test` ends `SENT` on the test relay.

Not verified at the time of writing: the image examples do not include the assess overlay,
so step 4 needs the overlay's services pointed at the published image (`image:` of
`vektra-assess-worker` and `vektra-assess-parser` is `vektra-stack`, the locally built tag).

## 5. After the release

- [ ] GitHub Release created from the CHANGELOG section (CONTRIBUTING.md).
- [ ] The pilot go/no-go document lists the exact Vektra and plugin versions tested together
      ([go/no-go](../operators/assess-pilot-go-no-go.md)).
