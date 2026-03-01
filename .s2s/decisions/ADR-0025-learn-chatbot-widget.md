# ADR-0025: Learn chatbot widget as backend-served JS bundle

**Status**: accepted
**Date**: 2026-03-01
**Context**: OQ-018 resolution (learn-ui architecture)

## Context

vektra-learn (Phase 2) exposes LMS-agnostic APIs for the e-learning vertical. Students interact with the system through a chatbot widget embedded in LMS pages. vektra-moodle (separate PHP repo) integrates the chatbot into Moodle via a plugin. But the chatbot must also work outside Moodle (other LMS, standalone pages).

The question is how to package and distribute the chatbot widget.

Phase 3 plans include vektra-sdk-js (published to npm, independent versioning). The chatbot widget will eventually depend on this SDK. But in Phase 2, vektra-sdk-js does not exist.

## Decision

Phase 2: serve the chatbot as a **self-contained JS bundle** from the vektra-learn backend at `/static/vektra-chat.js`. Integration is a single script tag.

Phase 3: migrate the widget to a **standalone npm package** (`@vektra/chat-widget`) in a separate repository, using vektra-sdk-js as a dependency.

### Phase 2 integration

```html
<script
  src="https://vektra.example.com/static/vektra-chat.js"
  data-api-url="https://vektra.example.com"
  data-course-id="CS101"
  data-token="eyJ..."
></script>
```

The widget:
- Renders a chat interface (floating button + expandable panel)
- Communicates with the backend exclusively via REST API (`POST /api/v1/query`)
- Accepts all configuration via `data-*` attributes on the script tag
- Has zero dependencies on server-side templates, cookies, or global variables
- Is framework-agnostic (vanilla JS or Preact, ~30KB bundled)

### Design constraints for Phase 3 migration

To ensure a clean migration from backend-served bundle to npm package:

1. **No server-side coupling.** The widget must not depend on Jinja2 variables, server-side session state, or backend-injected globals. All configuration via `data-*` attributes.

2. **REST API only.** The widget communicates with the backend through the public REST API (`POST /api/v1/query`, `GET /api/v1/conversations/{id}`). No internal endpoints, no shortcuts.

3. **Self-contained build artifact.** The widget is a single JS file (or JS + CSS) that can be extracted from the backend repo and published to npm without structural changes.

4. **Configuration contract stable.** The `data-*` attribute API (api-url, course-id, token, theme, language) becomes the npm package's configuration API. No breaking changes between Phase 2 script tag and Phase 3 npm import.

When Phase 3 introduces the npm package:
- Extract the widget source from vektra-learn
- Replace the inline API client with vektra-sdk-js
- Publish to npm as `@vektra/chat-widget`
- vektra-learn continues serving the bundle for backward compatibility (or redirects to CDN)

### vektra-moodle integration

The Moodle plugin (PHP, separate repo) integrates the chatbot by:
1. Generating a JWT token for the authenticated student
2. Injecting the script tag with `data-api-url`, `data-course-id`, `data-token`
3. The widget handles everything else client-side

This integration pattern is identical in Phase 2 (script tag from backend) and Phase 3 (script tag from npm CDN or self-hosted).

## Options considered

### Option 1: JS bundle served by backend - chosen for Phase 2

- Pro: unified deployment, no extra repo, no npm publish, versioning aligned with backend
- Pro: sufficient for vektra-moodle (PHP plugin includes script tag)
- Pro: no dependency on vektra-sdk-js (which does not exist yet in Phase 2)
- Con: not distributable as independent package
- Con: customization requires backend re-deploy

### Option 2: npm package standalone (separate repo)

- Pro: distributable independently, versioning independent from backend
- Pro: aligned with vektra-sdk-js (Phase 3)
- Con: premature without vektra-sdk-js (would duplicate API client logic)
- Con: separate repo, CI, release cycle overhead
- Con: coordination burden between widget version and backend version

### Option 3: iframe embed

- Pro: total CSS/JS isolation, trivial integration
- Con: UX limitations (fixed dimensions, scroll/focus issues on mobile)
- Con: poor mobile experience (webview inside iframe inside Moodle mobile)
- Con: no communication with host page without postMessage

## Consequences

- No npm/node dependency introduced in Phase 2 for the widget
- Widget source lives in vektra-learn repo under `static/` or `widget/`
- Build step (if using Preact): esbuild or similar, output is a single JS file
- Phase 3 migration is an extraction (move source to new repo + add vektra-sdk-js)
- vektra-moodle integration pattern is stable across Phase 2 and Phase 3

## Traceability

- Resolves: OQ-018 (learn-ui architecture)
- References: ARCH-063, CONTEXT.md component relationships (vektra-learn, vektra-moodle)
- Phase 3: extract to @vektra/chat-widget npm package, depends on vektra-sdk-js
