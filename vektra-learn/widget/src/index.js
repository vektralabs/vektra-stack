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
  // Find the current script tag to read data-* attributes
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

  const client = new ApiClient(apiUrl, token, courseId);

  const ui = new ChatUI({
    theme,
    language,
    onSend(question) {
      const msgEl = ui.createStreamMessage();

      client.queryStream(question, {
        onToken(tokenText) {
          ui.appendToken(msgEl, tokenText);
        },
        onSources(sources) {
          ui.addSources(msgEl, sources);
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
})();
