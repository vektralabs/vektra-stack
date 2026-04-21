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
 *     data-powered-by="false"
 *   ></script>
 *
 * White-label attributes (all optional):
 *   data-title             - header title and button aria-label
 *   data-primary-color     - hex/rgb/named color used for buttons and accents
 *   data-icon              - emoji or image URL for the floating button
 *   data-welcome-message   - assistant message shown on first open
 *   data-powered-by        - "true" (default) shows Vektra attribution,
 *                            "false" hides it
 */

import { ApiClient } from "./api-client.js";
import { ChatUI } from "./chat-ui.js";

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

  if (!apiUrl || !courseId || !token) {
    console.error(
      "[vektra-chat] Missing required attributes: data-api-url, data-course-id, data-token"
    );
    return;
  }

  function init() {
    const client = new ApiClient(apiUrl, token, courseId, { tokenRefreshUrl });

    const ui = new ChatUI({
      theme,
      language,
      customTitle,
      customPrimaryColor,
      customIcon,
      welcomeMessage,
      showPoweredBy,
      onSend(question) {
        const stream = ui.createStreamMessage();

        client.query(question, {
          onToken(tokenText) {
            ui.appendToken(stream, tokenText);
          },
          onSources(sources) {
            ui.addSources(stream, sources);
          },
          onNoRelevantContext() {
            ui.appendToken(stream, ui.noRelevantContextMessage());
          },
          onDone() {
            ui.doneSending();
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
    });

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

  // Wait for DOM to be ready before creating UI elements
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
