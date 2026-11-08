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
   * @param {object} [opts]
   * @param {function} [opts.onTokenExpired] - async callback returning a new token string
   * @param {string} [opts.tokenRefreshUrl] - URL to fetch a new token (POST, returns {token})
   */
  constructor(apiUrl, token, courseId, opts = {}) {
    this._apiUrl = apiUrl.replace(/\/+$/, "");
    this._token = token;
    this._courseId = courseId;
    this._conversationId = null;
    this._onTokenExpired = opts.onTokenExpired || null;
    this._tokenRefreshUrl = opts.tokenRefreshUrl || null;
    this._refreshing = false;
  }

  /** Update the token (used after refresh). */
  setToken(token) {
    this._token = token;
  }

  get conversationId() {
    return this._conversationId;
  }

  /**
   * Manually set the current conversation ID (e.g. restored from sessionStorage).
   * Use null to clear it.
   */
  getConversationId() {
    return this._conversationId;
  }

  setConversationId(id) {
    this._conversationId = id || null;
  }

  /**
   * Fetch decrypted turns for a stored conversation (WI-1 / FEAT-004).
   * Returns null on 404/403 (so the caller can reset local state silently),
   * throws on other errors so that a transient network failure does not
   * wipe the sessionStorage entry.
   *
   * @param {string} conversationId
   * @returns {Promise<{conversation_id: string, namespace: string, turns: Array}|null>}
   */
  async getConversationTurns(conversationId, _retried = false) {
    // Bounded timeout so a stalled backend doesn't block widget init.
    // AbortSignal.timeout rejects the fetch with an AbortError; callers
    // already handle thrown errors by leaving stored state untouched.
    const resp = await fetch(
      `${this._apiUrl}/api/v1/learn/conversations/${encodeURIComponent(conversationId)}/turns`,
      {
        method: "GET",
        headers: { Authorization: `Bearer ${this._token}` },
        signal: AbortSignal.timeout(8000),
      }
    );
    if (resp.status === 401 && !_retried) {
      const newToken = await this._refreshToken();
      if (newToken) {
        this._token = newToken;
        return this.getConversationTurns(conversationId, true);
      }
    }
    if (resp.status === 404 || resp.status === 403) {
      return null; // caller clears local state, no error surfaced to the user
    }
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    return resp.json();
  }

  /**
   * List the student's own conversations for this course (FEAT-027).
   *
   * Server-side ownership is what makes history survive a closed tab or a
   * different device: the id no longer has to come from sessionStorage.
   * Returns [] when the endpoint is unavailable or the deployment has no
   * persistent conversation store, so a widget on such a deployment behaves
   * exactly as it did before rather than erroring.
   *
   * @param {number} [limit]
   * @returns {Promise<Array<{conversation_id: string, title: string|null, turn_count: number, updated_at: string}>>}
   */
  async listConversations(limit = 5, _retried = false) {
    let resp;
    try {
      resp = await fetch(
        `${this._apiUrl}/api/v1/learn/conversations?limit=${encodeURIComponent(limit)}`,
        {
          method: "GET",
          headers: { Authorization: `Bearer ${this._token}` },
          signal: AbortSignal.timeout(8000),
        }
      );
    } catch {
      return [];
    }
    if (resp.status === 401 && !_retried) {
      const newToken = await this._refreshToken();
      if (newToken) {
        this._token = newToken;
        return this.listConversations(limit, true);
      }
    }
    if (!resp.ok) return [];
    try {
      const payload = await resp.json();
      return Array.isArray(payload?.conversations) ? payload.conversations : [];
    } catch {
      return [];
    }
  }

  /**
   * Delete one of the student's own conversations (FEAT-027, REQ-057).
   * Returns true when the server confirms the deletion.
   *
   * @param {string} conversationId
   * @returns {Promise<boolean>}
   */
  async deleteConversation(conversationId, _retried = false) {
    let resp;
    try {
      resp = await fetch(
        `${this._apiUrl}/api/v1/learn/conversations/${encodeURIComponent(conversationId)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${this._token}` },
          signal: AbortSignal.timeout(8000),
        }
      );
    } catch {
      return false;
    }
    if (resp.status === 401 && !_retried) {
      const newToken = await this._refreshToken();
      if (newToken) {
        this._token = newToken;
        return this.deleteConversation(conversationId, true);
      }
    }
    // 204 is the delete; 404 means it is already gone, which is the same
    // outcome for the student and must not surface as an error. The server
    // answers 404 for "not yours" too, and that is deliberate there: it must
    // not confirm which conversation ids exist.
    return resp.status === 204 || resp.status === 404;
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
  async query(question, callbacks, _retried = false) {
    const { onToken, onSources, onDone, onError, onNoRelevantContext } = callbacks;
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
        // Token expired: attempt refresh and retry once
        if (response.status === 401 && !_retried) {
          const newToken = await this._refreshToken();
          if (newToken) {
            this._token = newToken;
            return this.query(question, callbacks, true);
          }
        }
        const errData = await response.json().catch(() => ({}));
        const serverMessage = errData?.error?.message;
        const msg =
          response.status === 401
            ? `HTTP 401${serverMessage ? `: ${serverMessage}` : ""}`
            : serverMessage || `HTTP ${response.status}`;
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
              const payload = line.slice(6);
              if (payload.trim() === "[DONE]") {
                if (!receivedTokens && onNoRelevantContext) {
                  onNoRelevantContext();
                }
                if (onDone) onDone();
                return;
              }
              try {
                const event = JSON.parse(payload);
                if (event.type === "token" && onToken) {
                  onToken(event.data);
                  receivedTokens = true;
                } else if (event.type === "sources" && onSources) {
                  // FEAT-014: forward the server-resolved show_sources hint
                  // alongside the sources list (undefined on older servers).
                  onSources(event.data, event.show_sources);
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
          // FEAT-014: pass server-resolved show_sources hint to caller.
          onSources(data.sources, data.show_sources);
        }
        if (onDone) onDone();
      }
    } catch (err) {
      onError(err.message || "Network error");
    }
  }

  /**
   * Attempt to refresh the token via callback or URL.
   * @returns {Promise<string|null>} new token or null if refresh failed
   */
  async _refreshToken() {
    this._refreshing = true;
    try {
      // Callback takes priority
      if (this._onTokenExpired) {
        const token = await this._onTokenExpired();
        return typeof token === "string" && token ? token : null;
      }
      // URL-based refresh
      if (this._tokenRefreshUrl) {
        const resp = await fetch(this._tokenRefreshUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
        });
        if (resp.ok) {
          const data = await resp.json();
          return data.token || null;
        }
      }
      return null;
    } catch {
      return null;
    } finally {
      this._refreshing = false;
    }
  }
}
