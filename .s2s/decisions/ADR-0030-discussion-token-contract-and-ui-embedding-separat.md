# ADR-0030: Discussion token contract and UI embedding: separate key and aud, site-bound scopes, positive login-as, epoch revocation, transcript download token, cross-origin iframe with fetch streaming

**Status**: accepted
**Date**: 2026-11-01
**Context**: design session `20261101-143802-design-assessment-discussion-chatbot` (assessment discussion chatbot); covers ARCH-067, ARCH-076

## ARCH-067: Discussion token contract: one format, two binding states; separate key and aud; site-bound narrow scopes; positive login-as; epoch revocation; no v1 preview

### Context

Today the plugin mints widget tokens (HS256, shared secret, 1-24 h, no audience) with an
ADMIN-scope API key. That key can mint tokens for any student and can patch namespace config.
For discussions this would make a plugin config leak equivalent to impersonating every
student and forging oral-exam evidence. P3's "reject if loginas flag present" fails open.

### Decision

Approved content stands. Additions:
- TRANSCRIPT DOWNLOAD TOKEN: POST /api/v1/assess/transcripts/{tid}/download-token, minted
  server-side by the plugin under assess:mint (owner) or assess:teacher (graders).
  aud=transcript, single-use jti, TTL <= 2 min, bound to tid and sub. Session tokens are never
  kept alive for downloads.
- A minimum plugin_version is enforced at mint.
- Mint-to-Start audit correlation detects tokens not originating from the site's mint key
  (accepted residual risk: the API is the token issuer).

### Rationale

CONF-006 resolved by SEC's concession. The single format keeps the pre-Start single-use property. TL showed that a launch-stage WS check would put WS on the API path.

### Options considered

- **Separate key/audience + narrow scopes (adopted direction)** — pros: no cross-audience acceptance by construction; widget key rotation does not kill exam sessions; a plugin leak no longer gives Vektra admin; cons: one more secret with a rotation runbook; coordinated plugin and Vektra release
- **Reuse the widget mint path, key and admin scope (P3 as written)** — pros: zero new plumbing; cons: admin key becomes the identity root; cross-audience risk; login-as fails open

### Consequences

- (+) NFR-014 binding with immediate revocation
- (+) REQ-084 fail-closed
- (−) new auth scope in the ADR-0010 layer
- (−) XSS on the results page still exposes the short-lived token (limited by TTL and binding)

## ARCH-076: Discussion UI as a Vektra-origin page in a cross-origin iframe on a distinct origin

### Context

CONF-004. A bundle in the Moodle DOM shares the results-page origin (teacher-authored HTML, filters, third-party plugins). An XSS there could read the token and rewrite the REQ-098 core sentences.

### Decision

Approved content stands. Amendment, fixing a defect: native EventSource cannot send the
Authorization header. The iframe therefore consumes the event stream with fetch() and a
ReadableStream, sending the bearer header, with the SSE wire format unchanged. No route ever
accepts a token from the query string, which is asserted in CI.

### Rationale

Resolved (TL, SA, SEC; DevOps no objection).

### Consequences

- (+) token, bot output and core sentences are off the Moodle origin
- (+) no Moodle session lock in the streaming path
- (−) per-site frame-ancestors configuration
- (−) a TLS Vektra origin reachable from student browsers is required

