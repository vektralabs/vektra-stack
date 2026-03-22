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
 *   ></script>
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

  if (!apiUrl || !courseId || !token) {
    console.error(
      "[vektra-chat] Missing required attributes: data-api-url, data-course-id, data-token"
    );
    return;
  }

  function init() {
    const client = new ApiClient(apiUrl, token, courseId);

    const ui = new ChatUI({
      theme,
      language,
      onSend(question) {
        const msgEl = ui.createStreamMessage();

        client.query(question, {
          onToken(tokenText) {
            ui.appendToken(msgEl, tokenText);
          },
          onSources(sources) {
            ui.addSources(msgEl, sources);
          },
          onNoRelevantContext() {
            ui.appendToken(msgEl, ui.noRelevantContextMessage());
          },
          onDone() {
            ui.doneSending();
          },
          onError(errMsg) {
            ui.showError(errMsg);
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
