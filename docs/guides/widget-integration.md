# Widget integration guide

The Vektra RAG chatbot widget is a self-contained vanilla-JS bundle that embeds course-scoped Q&A into any LMS or web page. This guide covers the full integration path: server-side token issuance, embedding the widget, all configuration attributes, and behaviors like conversation persistence, token refresh, and source citations.

The widget is **LMS-agnostic**: it works with Moodle, Canvas, Blackboard, custom platforms, or plain HTML. For the Moodle plugin (which automates the integration), see [vektralabs/vektra-moodle](https://github.com/vektralabs/vektra-moodle).

## Architecture

The widget runs entirely client-side. All API calls go directly from the student's browser to the Vektra RAG backend, authenticated by a short-lived JWT that the LMS backend issues on the user's behalf.

```text
Student's browser
  ├── widget bundle (vektra-chat.js)
  └── HTTP/SSE → Vektra RAG /api/v1/learn/* (JWT auth)

LMS backend (Moodle, custom, ...)
  └── HTTP → Vektra RAG /api/v1/learn/tokens (API key auth, server-side only)
            ← {"token": "<JWT>", "expires_at": "..."}
```

The API key never leaves the LMS backend. The browser only sees the JWT, which is scoped to a single course (namespace) and short-lived. If the JWT expires, the widget can refresh it transparently via a callback or refresh URL exposed by the LMS.

## Quick start

### 1. Issue a JWT (server-side)

The LMS backend exchanges its admin API key for a course-scoped JWT:

```bash
curl -X POST https://your-vektra-host/api/v1/learn/tokens \
  -H "X-API-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"namespace": "CS101"}'
```

Response:

```json
{
  "token": "eyJ...",
  "expires_at": "2026-04-26T16:30:00Z"
}
```

The JWT carries the `course_id` claim and is signed with `VEKTRA_LEARN_JWT_SECRET`. See [API reference > Learn](../reference/api.md#learn) for the full request/response shape.

### 2. Embed the widget (HTML)

Drop a single `<script>` tag with the JWT into any LMS course page:

```html
<script
  src="https://your-vektra-host/static/learn/vektra-chat.js"
  data-api-url="https://your-vektra-host"
  data-course-id="CS101"
  data-token="eyJ..."
></script>
```

That's the minimum. The floating chat button appears in the bottom-right of the page; clicking it opens the chat panel.

## Configuration: `data-*` attributes

All customization lives on the `<script>` tag. No rebuild is needed to white-label the widget per course.

### Required

| Attribute | Description |
|---|---|
| `data-api-url` | Vektra RAG base URL the widget calls (e.g. `https://vektra.example.com`). |
| `data-course-id` | Stable course identifier. Also used as the `sessionStorage` key for conversation persistence. |
| `data-token` | JWT dashboard token issued by `POST /api/v1/learn/tokens`. |

### White-label (FEAT-016)

| Attribute | Default | Effect |
|---|---|---|
| `data-title` | "Course Assistant" / i18n | Chat panel header text and floating-button `aria-label`. |
| `data-primary-color` | `#2563eb` | Bound to CSS var `--vektra-primary`. Drives the button, accents, and links. Hover states use `filter: brightness()` so any custom color stays interactive. |
| `data-icon` | speech-bubble emoji | Floating button icon. Accepts an emoji (rendered as text) or a URL (rendered as `<img>`). |
| `data-welcome-message` | (none) | First assistant message shown when the widget opens. |
| `data-powered-by` | `true` | When `false` (case-insensitive), hide the attribution footer. |
| `data-powered-by-text` | "Powered by VektraLabs" | Override the footer text (plain text only). |
| `data-powered-by-url` | `https://vektralabs.github.io` | Override the footer link target. |
| `data-show-sources` | (server-resolved) | Client-side override for the citations block (FEAT-014). See [Source citations](#source-citations). |

### Behavior

| Attribute | Default | Effect |
|---|---|---|
| `data-theme` | `light` | Color theme: `light` or `dark`. |
| `data-language` | `en` | UI strings language: `en`, `it`. |
| `data-token-refresh-url` | (none) | URL the widget POSTs to after a `401` response, to obtain a refreshed JWT. See [Token refresh](#token-refresh). |

All `data-*` values are rendered via `textContent` (never `innerHTML`), `data-primary-color` is whitelist-validated, and `data-powered-by-url` / `data-token-refresh-url` are scheme-validated. XSS via attribute injection is neutralized.

## Conversation persistence (FEAT-004)

The widget keeps the active `conversation_id` in `sessionStorage` keyed by `course_id`. Reloading the page restores the chat history without leaking conversations across students on shared university computers (`sessionStorage` is tab-scoped and wiped on tab close; `localStorage` is intentionally not used).

On widget init, if a stored conversation is present and not stale (24h cutoff), the widget calls `GET /api/v1/learn/conversations/{id}/turns` and replays the history before enabling input. The header "New chat" button resets the storage entry, the DOM, and the in-memory `conversation_id`.

A `404` response from the turns endpoint (conversation deleted server-side) silently clears the entry. A `403` (different course/namespace) does the same.

## Token refresh (FEAT-009)

JWTs expire. Two patterns let the widget recover transparently after a `401`:

### Pattern A — `data-token-refresh-url`

Configure a URL on the LMS that issues a fresh JWT. After a `401`, the widget POSTs to that URL with `credentials: same-origin` and no body:

```html
<script
  src=".../vektra-chat.js"
  data-api-url="..."
  data-course-id="CS101"
  data-token="eyJ..."
  data-token-refresh-url="/lms/api/refresh-vektra-token"
></script>
```

The endpoint must return JSON `{"token": "<new-jwt>"}`. Any non-2xx status, malformed shape, or network error aborts the refresh and surfaces the original `401` to the user.

The refresh endpoint validates the LMS user's session (cookie auth), then internally calls `POST /api/v1/learn/tokens` with the LMS's admin API key. The API key never reaches the browser.

### Pattern B — `onTokenExpired` callback

For programmatic embeds (not data-attribute driven), the widget exposes an `onTokenExpired` callback. See `vektra-learn/widget/src/index.js` for the entry-point shape.

## Source citations (FEAT-014)

The widget renders source citations under each assistant answer when sources are present. Visibility is controlled by a 4-level resolution chain (highest priority first):

1. **`data-show-sources` attribute** — client-side override. The trimmed lowercase value is compared to `"false"`: only that exact string hides the section. Any other value (including `"true"`) forces it visible.
2. **`namespaces.config.show_sources`** — per-course server config, set via `PATCH /api/v1/admin/namespaces/{id}/config`.
3. **`VEKTRA_LEARN_SHOW_SOURCES`** environment variable — global default for the deployment.
4. **Hardcoded `true`** — final fallback.

The API always returns the full sources list regardless of visibility, so analytics and `QueryTrace` keep complete data. Visibility is purely a UI concern.

When sources are hidden, the answer still displays normally. When shown, sources are collapsible (per the accessibility refactor in v0.3.0) with `aria-controls` / `aria-expanded`.

## Error states

The widget surfaces explicit feedback for non-happy-path scenarios:

| State | When | UI |
|---|---|---|
| `unavailable` | Initial health check fails or repeated network errors. | Banner "The assistant is currently unavailable. Please try again later." Send button disabled. |
| `reconnecting` | Background re-check after `unavailable`. | Banner "Reconnecting...". |
| `sessionExpired` | `401` after refresh attempt fails (or no refresh URL configured). | Banner "Your session has expired. Please reload the page." Input disabled. |
| `noRelevantContext` | Backend reports no chunks above the relevance threshold. | Inline message "I couldn't find relevant information in the course materials for this question." |
| Generic error | Any other failure during a query. | Inline red message, last assistant message preserved. |

All strings are i18n-localized (`en`, `it`). Add a language by extending `I18N` in `vektra-learn/widget/src/chat-ui.js` and rebuilding the bundle.

## End-to-end example: PHP (Moodle-style)

```php
<?php
// Server-side: issue JWT for the current student in this course.
$ch = curl_init('https://vektra.example.com/api/v1/learn/tokens');
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'X-API-Key: ' . VEKTRA_ADMIN_KEY,    // never sent to browser
    'Content-Type: application/json',
]);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode([
    'namespace' => $course->idnumber,
]));
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
$response = json_decode(curl_exec($ch), true);
$token = $response['token'];
?>

<!-- Client-side: embed widget with the JWT -->
<script
  src="https://vektra.example.com/static/learn/vektra-chat.js"
  data-api-url="https://vektra.example.com"
  data-course-id="<?= htmlspecialchars($course->idnumber) ?>"
  data-token="<?= htmlspecialchars($token) ?>"
  data-token-refresh-url="/local/vektra/refresh.php?course=<?= $course->id ?>"
  data-language="<?= current_language() ?>"
  data-title="<?= get_string('course_assistant', 'local_vektra') ?>"
  data-primary-color="#1e3a8a"
></script>
```

The Moodle plugin ([vektra-moodle](https://github.com/vektralabs/vektra-moodle)) automates this flow as a course block.

## End-to-end example: Python (FastAPI)

```python
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()
VEKTRA_HOST = "https://vektra.example.com"
ADMIN_KEY = "..."

@app.get("/course/{course_id}", response_class=HTMLResponse)
async def course_page(course_id: str, request: Request):
    # Validate user's session here (omitted)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{VEKTRA_HOST}/api/v1/learn/tokens",
            headers={"X-API-Key": ADMIN_KEY},
            json={"namespace": course_id},
        )
        token = r.json()["token"]
    return f"""
    <html>
      <body>
        <h1>Course {course_id}</h1>
        <script
          src="{VEKTRA_HOST}/static/learn/vektra-chat.js"
          data-api-url="{VEKTRA_HOST}"
          data-course-id="{course_id}"
          data-token="{token}"
        ></script>
      </body>
    </html>
    """
```

## Security notes

- All `data-*` values are rendered via `textContent`. No XSS via attribute injection.
- `data-primary-color` is whitelist-validated against safe CSS color formats (hex, `rgb[a]`, `hsl[a]`, named colors). Anything else is silently ignored.
- `data-powered-by-url` and `data-token-refresh-url` accept only `http://`, `https://`, or same-origin paths. `javascript:`, `data:`, and other schemes are rejected.
- The JWT is embedded in HTML and visible to the student. This is by design: the JWT is course-scoped and short-lived; the API key stays server-side.
- `sessionStorage` is preferred over `localStorage` to avoid cross-student conversation leakage on shared computers.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Floating button doesn't appear | Missing required `data-*` attribute | Browser console will log `[vektra-chat] Missing required attributes: data-api-url, data-course-id, data-token`. |
| "Session expired" appears | JWT expired and no refresh path configured | Configure `data-token-refresh-url` or `onTokenExpired`. |
| "Unavailable" banner | Health check failing | Verify `data-api-url` is reachable from the student's browser; check CORS on the Vektra host. |
| Sources never appear | `data-show-sources="false"` or namespace config | Inspect the resolution chain. `GET /api/v1/admin/namespaces/{id}/config` returns the `resolved.show_sources` value. |
| Custom color ignored | `data-primary-color` failed whitelist | Use a hex, `rgb()`, `hsl()`, or named color. No quotes, semicolons, or whitespace. |

## Related documentation

- [API reference > Learn](../reference/api.md#learn) — endpoint specs
- [Configuration](../reference/configuration.md) — `VEKTRA_LEARN_*` environment variables
- [Error codes](../reference/error-codes.md) — `ERR-LEARN-*` and `ERR-ADMIN-*` series
- [vektra-learn component README](../../vektra-learn/README.md) — internal architecture
