# vektra-learn

E-learning vertical for the Vektra platform. Provides LMS-agnostic REST APIs for enrollment management, course-scoped RAG queries, and a chatbot widget.

## Components

- **Python backend** (`src/vektra_learn/`): FastAPI router with enrollment, content ingestion, token, conversation, and query endpoints
- **Chatbot widget** (`widget/`): Vanilla JS chat interface built with esbuild

## Widget build

```bash
cd widget && npm install && npm run build
```

Output: `static/vektra-chat.js`

## Widget integration

The widget is loaded via a `<script>` tag on the host LMS page. All customization is driven by `data-*` attributes on the script tag itself; no rebuild is needed to white-label the widget per course.

```html
<script
  src="https://your-vektra-host/static/vektra-chat.js"
  data-api-url="https://your-vektra-host"
  data-course-id="CS101"
  data-token="<jwt-dashboard-token>"
  data-token-refresh-url="/my-app/refresh-token"
  data-theme="light"
  data-language="en"
  data-title="Course Assistant"
  data-primary-color="#2563eb"
  data-icon="💬"
  data-welcome-message="Hi! Ask me anything about this course."
  data-powered-by="true"
  data-powered-by-text="Supported by University of X"
  data-powered-by-url="https://univ.example/help"
  data-show-sources="true"
></script>
```

### Required attributes

| Attribute | Description |
|-----------|-------------|
| `data-api-url` | Vektra base URL the widget calls (e.g. `https://vektra.example.com`). |
| `data-course-id` | Stable course identifier; also used as the `sessionStorage` key for conversation persistence. |
| `data-token` | JWT dashboard token issued by `POST /api/v1/learn/tokens` (server-side, by the LMS). |

### Optional white-label attributes (FEAT-016)

| Attribute | Default | Effect |
|-----------|---------|--------|
| `data-title` | "Course Assistant" / i18n | Chat panel header text and button aria-label. |
| `data-primary-color` | `#2563eb` | Bound to CSS var `--vektra-primary`. Drives the floating button, accents, and links. Hover states use `filter: brightness()` so any custom color stays interactive. |
| `data-icon` | speech-bubble emoji | Floating button icon. Accepts an emoji (rendered as text) or a URL (rendered as image). |
| `data-welcome-message` | (none) | First assistant message shown when the widget opens. |
| `data-powered-by` | `true` | When `false` (case-insensitive), hide the attribution footer. |
| `data-powered-by-text` | "Powered by Vektra" | Override the footer text (plain text only). |
| `data-powered-by-url` | `https://vektralabs.github.io` | Override the footer link target. |
| `data-show-sources` | (server-resolved) | Force or suppress the source-citations block (FEAT-014). Absent = defer to the server-resolved value. Resolution chain: this attr > `namespaces.config.show_sources` > `VEKTRA_LEARN_SHOW_SOURCES` env > hardcoded `true`. |

### Other attributes

| Attribute | Default | Effect |
|-----------|---------|--------|
| `data-theme` | `light` | Color theme: `light` or `dark`. |
| `data-language` | `en` | UI strings language: `en`, `it`. |
| `data-token-refresh-url` | (none) | Endpoint the widget calls when the JWT is about to expire (FEAT-009). |

All `data-*` values are rendered via `textContent` (never `innerHTML`), and `data-primary-color` is validated against a safe whitelist, so XSS via attribute injection is neutralized.

## Conversation persistence (FEAT-004)

The widget keeps the active `conversation_id` in `sessionStorage` keyed by `course_id`, so reloading the page restores the chat history without leaking conversations across students on shared university computers (sessionStorage is tab-scoped, wiped on tab close — `localStorage` is intentionally not used).

On widget init, if a stored conversation is present and not stale (24h cutoff), the widget calls `GET /api/v1/learn/conversations/{id}/turns` and replays the history before enabling input. The header "New chat" button resets the storage entry, the DOM, and the in-memory `conversation_id`. A `404` response from the turns endpoint (conversation deleted server-side) silently clears the entry; a `403` (different course/namespace) does the same.

## Endpoints

See [API reference > Learn](../docs/reference/api.md#learn) for full request/response shapes:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/v1/learn/tokens` | API key (`admin`) | Issue a JWT dashboard token (called server-side by the LMS). |
| `POST` | `/api/v1/learn/enrollments` | API key (`admin`) | Register a student in a course (optional when `VEKTRA_LEARN_REQUIRE_ENROLLMENT=false`). |
| `GET` | `/api/v1/learn/enrollments` | API key (`admin`) | List enrollments. |
| `DELETE` | `/api/v1/learn/enrollments/{id}` | API key (`admin`) | Remove an enrollment. |
| `POST` | `/api/v1/learn/content/ingest` | API key (`ingest`/`admin`) | Trigger course-scoped ingestion. |
| `POST` | `/api/v1/learn/query` | JWT | Course-scoped RAG query. Returns `show_sources` flag + `document_name` per source. |
| `GET` | `/api/v1/learn/conversations/{id}/turns` | JWT | Replay decrypted conversation history (namespace-scoped; emits `learn_conversation_turns_read` audit row per NFR-007). |
