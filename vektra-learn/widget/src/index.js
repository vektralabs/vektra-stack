/**
 * Vektra Chat Widget - Entry point.
 *
 * Reads configuration from data-* attributes on the <script> tag,
 * initializes the API client and chat UI, and wires them together.
 *
 * Usage:
 *   <script src="/static/vektra-chat.js"
 *     data-api-url="https://vektra.example.com"
 *     data-course-id="CS101"
 *     data-token="eyJ..."
 *     data-theme="light"
 *     data-language="en"
 *     data-token-refresh-url="/my-app/refresh-token"
 *     data-title="My course helper"
 *     data-primary-color="#9333ea"
 *     data-icon="https://example.com/bot.png"
 *     data-welcome-message="Hi! Ask me anything about the course."
 *     data-powered-by="true"
 *     data-powered-by-text="Supported by University of X"
 *     data-powered-by-url="https://univ.example/help"
 *     data-show-sources="true"
 *   ></script>
 *
 * White-label attributes (all optional):
 *   data-title             - header title and button aria-label
 *   data-primary-color     - hex/rgb/named color used for buttons and accents
 *   data-icon              - emoji or image URL for the floating button
 *   data-welcome-message   - assistant message shown on first open
 *   data-powered-by        - "true" (default) shows the attribution footer,
 *                            "false" hides it
 *   data-powered-by-text   - overrides the footer text (plain text, no HTML).
 *                            Default: "Powered by VektraLabs" as a link.
 *   data-powered-by-url    - overrides the footer link target. Default:
 *                            https://vektralabs.github.io
 *   data-show-sources      - "true"/"false" client-side override for the
 *                            source citations section. Absent = defer to the
 *                            server-resolved value (namespace config > env
 *                            default). (FEAT-014)
 */

import { ApiClient } from "./api-client.js";
import { ChatUI } from "./chat-ui.js";

// Storage key for per-course conversation persistence (WI-5). Tab-scoped
// via sessionStorage so that shared university computers don't leak a
// student's conversation to the next user.
const STORAGE_PREFIX = "vektra-conv:";
// Tabs kept open for days shouldn't silently resurface yesterday's chat.
const STALE_AFTER_MS = 24 * 60 * 60 * 1000;

function _readStored(courseId) {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_PREFIX + courseId);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !parsed.conversation_id) return null;
    const age = Date.now() - (parsed.stored_at || 0);
    if (age < 0 || age > STALE_AFTER_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function _writeStored(courseId, conversationId) {
  try {
    window.sessionStorage.setItem(
      STORAGE_PREFIX + courseId,
      JSON.stringify({
        conversation_id: conversationId,
        stored_at: Date.now(),
      })
    );
  } catch {
    // Storage full or unavailable — silently ignore; persistence is a
    // convenience, not a correctness requirement.
  }
}

// A "new chat" or a deletion is a decision, not an accident: remember it for
// this tab so the server-side resume (FEAT-027) does not undo it on reload.
const FRESH_PREFIX = "vektra-fresh:";

function _markFreshStart(courseId) {
  try {
    window.sessionStorage.setItem(FRESH_PREFIX + courseId, String(Date.now()));
  } catch {
    // ignore
  }
}

function _startedFresh(courseId) {
  try {
    return window.sessionStorage.getItem(FRESH_PREFIX + courseId) !== null;
  } catch {
    return false;
  }
}

function _clearStored(courseId) {
  try {
    window.sessionStorage.removeItem(STORAGE_PREFIX + courseId);
  } catch {
    // ignore
  }
}

(function () {
  // Capture the script tag synchronously (before DOMContentLoaded)
  const scripts = document.querySelectorAll('script[src*="vektra-chat"]');
  const scriptTag = scripts[scripts.length - 1];

  if (!scriptTag) {
    console.error("[vektra-chat] Could not find script tag.");
    return;
  }

  const apiUrl = scriptTag.getAttribute("data-api-url");
  const courseId = scriptTag.getAttribute("data-course-id");
  const token = scriptTag.getAttribute("data-token");
  const theme = scriptTag.getAttribute("data-theme") || "light";
  const language = scriptTag.getAttribute("data-language") || "en";
  const tokenRefreshUrl = scriptTag.getAttribute("data-token-refresh-url") || null;

  // White-label attributes (all optional). See file header for semantics.
  const customTitle = scriptTag.getAttribute("data-title") || null;
  const customPrimaryColor =
    scriptTag.getAttribute("data-primary-color") || null;
  const customIcon = scriptTag.getAttribute("data-icon") || null;
  const welcomeMessage = scriptTag.getAttribute("data-welcome-message") || null;
  const poweredByAttr = scriptTag.getAttribute("data-powered-by");
  // Default ON; only "false" (case-insensitive) hides attribution.
  const showPoweredBy = poweredByAttr === null
    ? true
    : poweredByAttr.toLowerCase() !== "false";
  const poweredByText = scriptTag.getAttribute("data-powered-by-text") || null;
  const poweredByUrl = scriptTag.getAttribute("data-powered-by-url") || null;

  // FEAT-014: client-side override for source citation visibility. When the
  // attribute is absent, we defer to the server-resolved value that arrives
  // with each query response; when present, "false" hides citations and any
  // other string re-enables them (including an explicit "true" that forces
  // visibility even when the namespace config disables them).
  const showSourcesAttr = scriptTag.getAttribute("data-show-sources");
  // Trim before lowercasing so " false " (or surrounding whitespace from
  // server-side templating) is honoured, not silently treated as truthy.
  const clientShowSourcesOverride =
    showSourcesAttr === null
      ? null
      : showSourcesAttr.trim().toLowerCase() !== "false";

  if (!apiUrl || !courseId || !token) {
    console.error(
      "[vektra-chat] Missing required attributes: data-api-url, data-course-id, data-token"
    );
    return;
  }

  async function init() {
    const client = new ApiClient(apiUrl, token, courseId, { tokenRefreshUrl });

    const ui = new ChatUI({
      theme,
      language,
      customTitle,
      customPrimaryColor,
      customIcon,
      welcomeMessage,
      showPoweredBy,
      poweredByText,
      poweredByUrl,
      onSend(question) {
        const stream = ui.createStreamMessage();

        client.query(question, {
          onToken(tokenText) {
            ui.appendToken(stream, tokenText);
          },
          onSources(sources, serverShowSources) {
            // Resolution: client data-show-sources override > server hint >
            // default true (legacy behaviour when the server omits the field).
            const effective =
              clientShowSourcesOverride !== null
                ? clientShowSourcesOverride
                : typeof serverShowSources === "boolean"
                  ? serverShowSources
                  : true;
            if (effective) {
              ui.addSources(stream, sources);
            }
          },
          onNoRelevantContext() {
            ui.appendToken(stream, ui.noRelevantContextMessage());
          },
          onDone() {
            ui.doneSending();
            // Persist conversation ID for history restore on next load (WI-5).
            if (client.conversationId) {
              _writeStored(courseId, client.conversationId);
            }
          },
          onError(errMsg) {
            // Show session expired message for auth failures
            if (errMsg && errMsg.includes("HTTP 401")) {
              ui.setConnectionStatus("sessionExpired");
            } else {
              ui.showError(errMsg);
            }
            ui.doneSending();
          },
        });
      },
      onNewChat() {
        // Reset both storage and client-side state so the next query
        // creates a fresh conversation.
        _clearStored(courseId);
        client.setConversationId(null);
        // Server-side history exists now (FEAT-027), so "new chat" has to be
        // remembered for this tab: without the marker, the next load would
        // helpfully resume the very conversation the student just left.
        _markFreshStart(courseId);
      },
      async onDeleteHistory() {
        const target = client.getConversationId() || (await _resolveConversationId());
        if (!target) {
          // Nothing stored server-side: the local reset is the whole deletion.
          _clearStored(courseId);
          _markFreshStart(courseId);
          return true;
        }
        const deleted = await client.deleteConversation(target);
        if (deleted) {
          _clearStored(courseId);
          _markFreshStart(courseId);
          client.setConversationId(null);
        }
        return deleted;
      },
    });

    // Restore a conversation from a prior load (WI-5 / FEAT-004). Awaited,
    // with input+send disabled via ui.setRestoring(true) for the duration
    // of the fetch. This closes a race the earlier "just await" fix did
    // not fully handle: the UI is already mounted when the await starts,
    // so without the input lock a fast user could type+send during the
    // fetch — _handleSend would paint the user bubble into the DOM, then
    // replayTurns would clear it when the restore resolved, losing the
    // message. The fetch has an 8s timeout (api-client.js) so the input
    // lock never persists indefinitely. Any failure keeps the stored id
    // for the next load.
    // Which conversation to offer: the one this tab was using, or — when the
    // tab has none, which is every new device, browser or reopened window —
    // the student's most recent one on the server (FEAT-027). The server
    // knows it because conversations are owned by the token's subject; before
    // that, history could not outlive a sessionStorage entry.
    async function _resolveConversationId() {
      const stored = _readStored(courseId);
      if (stored) return stored.conversation_id;
      if (_startedFresh(courseId)) return null;
      const mine = await client.listConversations(1);
      return mine.length > 0 ? mine[0].conversation_id : null;
    }

    async function restoreConversation() {
      ui.setRestoring(true);
      try {
        const conversationId = await _resolveConversationId();
        if (!conversationId) return;
        const payload = await client.getConversationTurns(conversationId);
        if (payload && Array.isArray(payload.turns) && payload.turns.length > 0) {
          client.setConversationId(conversationId);
          _writeStored(courseId, conversationId);
          ui.replayTurns(payload.turns);
        } else {
          // Returned null (404/403) or empty turns — abandon stored id
          _clearStored(courseId);
        }
      } catch {
        // Network/transient error (incl. AbortError on timeout): keep
        // stored id, try again next load.
      } finally {
        ui.setRestoring(false);
      }
    }
    await restoreConversation();

    // Check API connectivity on startup and show status if unreachable
    let retryTimer = null;

    async function checkConnection() {
      const healthy = await client.checkHealth();
      if (healthy) {
        ui.setConnectionStatus(null);
        if (retryTimer) {
          clearInterval(retryTimer);
          retryTimer = null;
        }
      } else {
        ui.setConnectionStatus("unavailable");
        // Retry every 30s until connection is restored
        if (!retryTimer) {
          retryTimer = setInterval(async () => {
            ui.setConnectionStatus("reconnecting");
            const ok = await client.checkHealth();
            if (ok) {
              ui.setConnectionStatus(null);
              clearInterval(retryTimer);
              retryTimer = null;
            } else {
              ui.setConnectionStatus("unavailable");
            }
          }, 30000);
        }
      }
    }

    checkConnection();
  }

  // Wait for DOM to be ready before creating UI elements. init is async
  // (awaits restoreConversation); wrap with .catch so any unexpected
  // rejection surfaces in the console rather than as an unhandled promise.
  function runInit() {
    init().catch((err) => console.error("[vektra-chat] init failed:", err));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runInit);
  } else {
    runInit();
  }
})();
