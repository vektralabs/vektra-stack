/**
 * REST API client for the Vektra learn query endpoint.
 * Uses fetch with SSE streaming for token-by-token response display,
 * with JSON fallback for non-streaming responses.
 */

export class ApiClient {
  /**
   * @param {string} apiUrl - Base URL of the Vektra API
   * @param {string} token - JWT dashboard token
   * @param {string} courseId - Course identifier for scoped queries
   */
  constructor(apiUrl, token, courseId) {
    this._apiUrl = apiUrl.replace(/\/+$/, "");
    this._token = token;
    this._courseId = courseId;
    this._conversationId = null;
  }

  get conversationId() {
    return this._conversationId;
  }

  /**
   * Check if the Vektra API is reachable.
   * @returns {Promise<boolean>}
   */
  async checkHealth() {
    try {
      const resp = await fetch(`${this._apiUrl}/health`, {
        method: "GET",
        signal: AbortSignal.timeout(5000),
      });
      return resp.ok;
    } catch {
      return false;
    }
  }

  /**
   * Send a query with SSE streaming support and JSON fallback.
   * @param {string} question
   * @param {object} callbacks - { onToken, onSources, onDone, onError, onNoRelevantContext }
   */
  async query(question, { onToken, onSources, onDone, onError, onNoRelevantContext }) {
    const body = {
      question,
      stream: true,
      top_k: 5,
    };
    if (this._conversationId) {
      body.conversation_id = this._conversationId;
    }

    try {
      const response = await fetch(
        `${this._apiUrl}/api/v1/learn/query`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${this._token}`,
          },
          body: JSON.stringify(body),
        }
      );

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        const msg =
          errData?.error?.message ||
          errData?.detail?.error?.message ||
          `HTTP ${response.status}`;
        onError(msg);
        return;
      }

      const contentType = response.headers.get("content-type") || "";

      // SSE stream
      if (contentType.includes("text/event-stream")) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let receivedTokens = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const payload = line.slice(6).trim();
              if (payload === "[DONE]") {
                if (!receivedTokens && onNoRelevantContext) {
                  onNoRelevantContext();
                }
                if (onDone) onDone();
                return;
              }
              // Plain text tokens (not JSON)
              if (!payload.startsWith("{")) {
                if (onToken) {
                  onToken(payload);
                  receivedTokens = true;
                }
                continue;
              }
              try {
                const event = JSON.parse(payload);
                if (event.type === "token" && onToken) {
                  onToken(event.data);
                  receivedTokens = true;
                } else if (event.type === "sources" && onSources) {
                  onSources(event.data);
                } else if (event.type === "done") {
                  if (event.data?.conversation_id) {
                    this._conversationId = event.data.conversation_id;
                  }
                  if (!receivedTokens && onNoRelevantContext) {
                    onNoRelevantContext();
                  }
                  if (onDone) onDone();
                  return;
                } else if (event.type === "error" && onError) {
                  onError(event.data);
                  return;
                }
              } catch {
                // Skip malformed JSON lines
              }
            }
          }
        }
        if (onDone) onDone();
      } else {
        // JSON fallback (non-streaming)
        const data = await response.json();
        if (data.conversation_id) {
          this._conversationId = data.conversation_id;
        }
        if (data.no_relevant_context && onNoRelevantContext) {
          onNoRelevantContext();
        } else if (onToken && data.answer) {
          onToken(data.answer);
        }
        if (onSources && data.sources && data.sources.length > 0) {
          onSources(data.sources);
        }
        if (onDone) onDone();
      }
    } catch (err) {
      onError(err.message || "Network error");
    }
  }
}
