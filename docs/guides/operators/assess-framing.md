# Assess: framing the discussion UI

The student discussion runs in an iframe on the LMS results page, but the page inside it is
served by Vektra on its own origin (ARCH-076). This runbook sets up that origin, the per-site
framing origin and the reverse proxy, and verifies them. The UI's headers and the postMessage
contract are in the [API reference](../../reference/api.md#assess-ui-the-discussion-page-and-the-postmessage-contract-plugin---ui).

## The three origins

| Origin | Setting | Serves |
|---|---|---|
| API | `VEKTRA_ASSESS_API_PUBLIC_ORIGIN` (e.g. `https://vektra.uni.example`) | the plugin's server-side calls (token mint, teacher, privacy), admin, health, every non-UI route; `X-Frame-Options: DENY` on every response |
| UI | `VEKTRA_ASSESS_UI_ORIGIN` (e.g. `https://assess.vektra-chat.example`) | `GET /api/v1/assess/ui`, its assets, and only the student routes the page calls on its own origin (CSP `connect-src 'self'`): `GET /state`, `/sessions...`, `GET /transcripts/{tid}`; no `X-Frame-Options` |
| LMS | each site's `lms_origin` (e.g. `https://moodle.uni.example`) | the page that frames the UI; the only origin allowed in the UI's CSP `frame-ancestors` and the only peer of its postMessage |

One backend serves both Vektra origins; the reverse proxy decides which routes each one exposes.

## The distinct-origin requirement

The plugin's iframe sandbox is `allow-scripts allow-same-origin allow-downloads`.
`allow-same-origin` is safe only because the framed page is on another origin than the LMS page
that frames it. The code enforces, and the setup check reports:

- `VEKTRA_ASSESS_UI_ORIGIN` is a bare origin (scheme, host, optional port; no path) and `https`
  outside development. Otherwise the API refuses to start (`ui_origin_invalid`,
  `ui_origin_not_https`).
- It is not equal to any site's LMS origin, and every LMS origin is `https` outside development
  (`ui_origin_equals_lms_origin`, `lms_origin_not_https`); the API refuses to start.
- It is another **site** than the API origin and than each LMS origin (the setup check items
  `ui_not_same_origin` and `ui_origin_distinct`, reasons `same_site_as_api`,
  `same_site_as_lms`, `same_site_as_lms_origin`). "Same site" here means the same host, one
  host a subdomain of the other, or the same last two host labels. The last rule is an
  approximation of the registrable domain: under a public suffix of more than one label (for
  example `ac.example-cc`) two different institutions' hosts can be refused as the same site.
  **This rule is enforced by the setup check only, when it runs**: the API's startup and the UI
  route check equality and `https` (the two items above), not same-site, so an API whose UI is
  on a subdomain of the API's domain starts and serves the page. A green report stays green until
  the next check, so **re-run the setup check after every change of `VEKTRA_ASSESS_UI_ORIGIN`,
  `VEKTRA_ASSESS_API_PUBLIC_ORIGIN` or a site's `lms_origin` / `base_url`**. In practice: put
  the UI on a domain of its own, not on a subdomain of the API's or the LMS's domain.

## Set each site's LMS origin

The framing origin is per site, stored in `assess_sites.lms_origin` (the assess base revision). No API
route writes it; set it as the database owner:

```sql
UPDATE assess_sites SET lms_origin = 'https://moodle.uni.example' WHERE id = '<site id>';
```

The column accepts only a bare origin (a CHECK constraint with the same pattern as the code,
`vektra_assess.ui.origins.ORIGIN_RE`). A site without it falls back to the origin of its
`base_url`. No restart is needed, but a change is not immediate: the UI route caches each
site's framing origin (or its absence) for 30 seconds per API process (`SITE_TTL_S` in
`vektra_assess/ui/router.py`), so for up to 30 s after an update the page still carries the old
`frame-ancestors`, and a site just disabled is still served. Wait 30 s before verifying. A
disabled or unknown site then gets `404` with `frame-ancestors 'none'`.

The API checks every site's origin at startup (equal to the UI origin, or not `https` outside
development: a startup refusal). A change made **after** startup is not a startup refusal until
the next restart: the UI route repeats the same check per request and, once the cache expires,
answers that site's page with `503 ERR-ASSESS-001` and the remediation naming `site_origin`.

## Reverse proxy

### nginx (shipped example)

`deploy/nginx/vektra.conf.example` has the two `server` blocks:

- the API server (`YOUR_DOMAIN`) adds `X-Frame-Options DENY` and answers `404` on
  `/api/v1/assess/ui`, so the page is never served on the API origin;
- the UI server (`YOUR_UI_DOMAIN`) adds no `X-Frame-Options` at all, at no level (nginx drops
  every inherited `add_header` as soon as a location declares one, so no location in it declares
  any), proxies `/api/v1/assess/ui`, `= /api/v1/assess/state`, `/api/v1/assess/sessions` (with
  `proxy_buffering off` for the event stream) and `GET /api/v1/assess/transcripts/{tid}` only
  (a regex location with `limit_except GET`: the `download-token` and `resend` POSTs are plugin
  calls and stay on the API origin), and answers `404` for everything else.

Two details the page depends on:

- The UI page answers only when the request's scheme and `Host` are exactly
  `VEKTRA_ASSESS_UI_ORIGIN`; otherwise it is `503 ERR-ASSESS-001` (`ui_origin`). The UI server
  therefore forwards `Host $http_host` (with the port; `$host` drops a non-default port) and
  `X-Forwarded-Proto $scheme`.
- If nginx runs in another container or host than the API, set uvicorn's `FORWARDED_ALLOW_IPS`
  to the proxy's address so the forwarded scheme is trusted.

The API itself already sets `X-Frame-Options: DENY` on every response except the UI page and its
assets (`vektra_app.main`), so a proxy that adds nothing still gets the API side right. What a
proxy must never do is add `X-Frame-Options` on the UI server: the browser would then refuse to
frame the page whatever its CSP says.

### Traefik

`deploy/traefik/docker-compose.traefik.yml.example` defines one router for `YOUR_DOMAIN` and no
router for the UI origin. **No Traefik configuration for the assess UI ships, and none has been
tested.** A Traefik deployment needs a second router on the UI host that exposes only the paths
listed above, forwards the client's `Host`, and adds no frame-related header middleware.

## Verify

1. The API starts. A wrong UI origin, or a wrong LMS origin already stored at startup, is a
   startup refusal. An LMS origin changed afterwards to the UI origin, or to `http` outside
   development, shows instead as `503 ERR-ASSESS-001` (`site_origin`) on that site's page, at
   most 30 s after the change. Wait 30 s after any `lms_origin` change before the next step.
2. Headers on the UI origin (no credential needed: the page is public and carries no data):

   ```bash
   curl -s -o /dev/null -D - "https://assess.vektra-chat.example/api/v1/assess/ui?site=$SITE_ID"
   ```

   Expect `200`, a `Content-Security-Policy` whose `frame-ancestors` is exactly the site's LMS
   origin, and **no** `X-Frame-Options` header.
3. The same path on the API origin is `404` (with the shipped nginx example), and any other
   route on the UI origin is `404`:

   ```bash
   curl -s -o /dev/null -D - "https://vektra.uni.example/api/v1/assess/ui?site=$SITE_ID"   # 404
   curl -s -o /dev/null -D - "https://assess.vektra-chat.example/health"                   # 404
   ```

4. Run the setup check (see [deployment](assess-deployment.md#setup-check-walkthrough)):
   `ui_not_same_origin` and `ui_origin_distinct` must both be `OK`. `ui_not_same_origin` is
   `UNVERIFIABLE` while `VEKTRA_ASSESS_API_PUBLIC_ORIGIN` is unset.
5. Open the activity in the LMS as a test student: the discussion iframe loads. A blank frame
   with a CSP or `X-Frame-Options` error in the browser console points to the proxy or to
   `lms_origin`.

Step 5 is a manual check; the automated equivalent is the browser e2e
(`make test-assess-e2e`), which frames the UI from a stub on a second origin and checks that a
third origin is refused. It runs against a local stack, not your proxy.
