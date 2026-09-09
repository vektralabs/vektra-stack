/**
 * Chat UI: floating button, expandable panel, message list, input.
 * Vanilla DOM manipulation, no framework dependencies.
 */

import { renderMarkdown } from "./markdown.js";
import { buildStyles } from "./styles.js";

const I18N = {
  en: {
    title: "Course Assistant",
    placeholder: "Ask a question...",
    send: "Send",
    sources: "Sources:",
    sourceFallback: "Source",
    noRelevantContext:
      "I couldn't find relevant information in the course materials for this question.",
    error: "An error occurred. Please try again.",
    unavailable: "The assistant is currently unavailable. Please try again later.",
    reconnecting: "Reconnecting...",
    sessionExpired: "Your session has expired. Please reload the page.",
    close: "Close",
    newChat: "New chat",
    poweredBy: "Powered by",
  },
  it: {
    title: "Assistente del corso",
    placeholder: "Fai una domanda...",
    send: "Invia",
    sources: "Fonti:",
    sourceFallback: "Fonte",
    noRelevantContext:
      "Non ho trovato informazioni rilevanti nei materiali del corso per questa domanda.",
    error: "Si è verificato un errore. Riprova.",
    unavailable: "L'assistente non è al momento disponibile. Riprova più tardi.",
    reconnecting: "Riconnessione...",
    sessionExpired: "La sessione è scaduta. Ricarica la pagina.",
    close: "Chiudi",
    newChat: "Nuova chat",
    poweredBy: "Offerto da",
  },
};

// Accept CSS colors that are hex (#rgb, #rgba, #rrggbb, #rrggbbaa),
// rgb[a](), hsl[a](), or named (alphanumeric). Rejects anything with
// quotes, semicolons, or whitespace that could break out into a style rule.
// Matches the conservative set proposed by the plan's XSS mitigation.
const _VALID_COLOR_RE =
  /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,\s%]+\)|hsla?\([0-9.,\s%]+\)|[a-zA-Z]+)$/;

function _isSafeColor(value) {
  return typeof value === "string" && _VALID_COLOR_RE.test(value.trim());
}

// An icon value that looks like a URL becomes an <img>; otherwise it is
// rendered as plain text (emoji or short string) via textContent to avoid
// HTML injection via data-icon.
function _iconIsUrl(value) {
  if (typeof value !== "string") return false;
  const v = value.trim();
  return (
    v.startsWith("http://") ||
    v.startsWith("https://") ||
    v.startsWith("/") ||
    v.startsWith("data:image/")
  );
}

// Only http(s) and same-origin paths are allowed as link targets. Rejects
// javascript:, data:, and other schemes that could enable XSS when the
// operator-controlled attribute is combined with custom text.
function _isSafeLinkUrl(value) {
  if (typeof value !== "string") return false;
  const v = value.trim();
  return (
    v.startsWith("http://") || v.startsWith("https://") || v.startsWith("/")
  );
}

export class ChatUI {
  /**
   * @param {object} opts
   * @param {string} opts.theme - "light" or "dark"
   * @param {string} opts.language - "en" or "it"
   * @param {string|null} [opts.customTitle] - overrides the localized title
   * @param {string|null} [opts.customPrimaryColor] - CSS color for accents
   * @param {string|null} [opts.customIcon] - emoji or image URL for the button
   * @param {string|null} [opts.welcomeMessage] - first assistant message on open
   * @param {boolean} [opts.showPoweredBy=true] - show attribution footer
   * @param {string|null} [opts.poweredByText] - custom footer text (plain text)
   * @param {string|null} [opts.poweredByUrl] - custom footer link target
   * @param {function} opts.onSend - callback(question: string)
   * @param {function} [opts.onNewChat] - callback invoked when "New chat" is clicked
   */
  constructor({
    theme = "light",
    language = "en",
    customTitle = null,
    customPrimaryColor = null,
    customIcon = null,
    welcomeMessage = null,
    showPoweredBy = true,
    poweredByText = null,
    poweredByUrl = null,
    onSend,
    onNewChat = null,
  }) {
    this._theme = theme;
    this._lang = I18N[language] || I18N.en;
    this._onSend = onSend;
    this._onNewChat = onNewChat;
    this._isOpen = false;
    this._sending = false;
    this._restoring = false; // true while a conversation history is being fetched
    this._status = null; // "unavailable" | "reconnecting" | "sessionExpired" | null

    this._title = customTitle || this._lang.title;
    this._icon = customIcon;
    this._welcomeMessage = welcomeMessage;
    this._showPoweredBy = showPoweredBy;
    this._poweredByText = poweredByText;
    this._poweredByUrl = poweredByUrl;
    this._welcomeShown = false;
    // Accept color only if it matches the conservative whitelist; anything
    // else is silently ignored (falls back to theme default) to prevent CSS
    // injection via data-primary-color.
    this._primaryColor = _isSafeColor(customPrimaryColor)
      ? customPrimaryColor.trim()
      : null;

    this._injectStyles();
    this._createElements();
    this._bindEvents();
  }

  _injectStyles() {
    const style = document.createElement("style");
    style.textContent = buildStyles(this._theme);
    document.head.appendChild(style);

    if (this._primaryColor) {
      // Reuse a single override node — only the primary-color override is
      // deduped, not the main style block above (DEBT-018 scope: avoid the
      // override leaking to host page :root and clobbering unrelated
      // --vektra-primary uses; main-style accumulation across hot reloads is
      // not tackled here, see DEBT-022 for multi-instance support).
      let override = document.getElementById("vektra-primary-override");
      if (!override) {
        override = document.createElement("style");
        override.id = "vektra-primary-override";
        document.head.appendChild(override);
      }
      override.textContent = `.vektra-chat-btn, .vektra-chat-panel { --vektra-primary: ${this._primaryColor}; }`;
    }
  }

  _createElements() {
    // Floating button (aria-label + icon set via safe APIs — no innerHTML of user input)
    this._btn = document.createElement("button");
    this._btn.className = "vektra-chat-btn";
    this._btn.setAttribute("aria-label", this._title);
    if (this._icon && _iconIsUrl(this._icon)) {
      const img = document.createElement("img");
      img.src = this._icon;
      img.alt = "";
      img.className = "vektra-chat-btn-icon";
      this._btn.appendChild(img);
    } else if (this._icon) {
      this._btn.textContent = this._icon;
    } else {
      this._btn.innerHTML = "&#128172;"; // speech bubble emoji
    }
    document.body.appendChild(this._btn);

    // Chat panel — scaffold built with safe APIs; title uses textContent below.
    this._panel = document.createElement("div");
    this._panel.className = "vektra-chat-panel";
    this._panel.innerHTML = `
      <div class="vektra-chat-header">
        <span class="vektra-chat-header-title"></span>
        <div class="vektra-chat-header-actions">
          <button class="vektra-chat-new" aria-label="${this._lang.newChat}" title="${this._lang.newChat}">&#10227;</button>
          <button class="vektra-chat-close" aria-label="${this._lang.close}">&times;</button>
        </div>
      </div>
      <div class="vektra-chat-messages"></div>
      <div class="vektra-chat-input-area">
        <input class="vektra-chat-input" type="text"
               placeholder="${this._lang.placeholder}"
               aria-label="${this._lang.placeholder}" />
        <button class="vektra-chat-send">${this._lang.send}</button>
      </div>
    `;
    // Title: set via textContent so data-title cannot inject HTML
    const titleEl = this._panel.querySelector(".vektra-chat-header-title");
    titleEl.textContent = this._title;

    if (this._showPoweredBy) {
      const footer = document.createElement("div");
      footer.className = "vektra-chat-powered-by";
      // If poweredByText is set, the whole footer is a single link with that
      // text. Otherwise the footer is "<i18n: Powered by> <link>VektraLabs</link>".
      // A blank or invalid poweredByUrl falls back to the VektraLabs default URL.
      const url = _isSafeLinkUrl(this._poweredByUrl)
        ? this._poweredByUrl.trim()
        : "https://vektralabs.github.io";
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";

      if (this._poweredByText) {
        link.textContent = this._poweredByText;
        footer.appendChild(link);
      } else {
        link.textContent = "VektraLabs";
        footer.textContent = `${this._lang.poweredBy} `;
        footer.appendChild(link);
      }
      this._panel.appendChild(footer);
    }

    document.body.appendChild(this._panel);

    // Cache references
    this._messagesEl = this._panel.querySelector(".vektra-chat-messages");
    this._inputEl = this._panel.querySelector(".vektra-chat-input");
    this._sendBtn = this._panel.querySelector(".vektra-chat-send");
    this._closeBtn = this._panel.querySelector(".vektra-chat-close");
    this._newChatBtn = this._panel.querySelector(".vektra-chat-new");
  }

  _bindEvents() {
    this._btn.addEventListener("click", () => this.toggle());
    this._closeBtn.addEventListener("click", () => this.close());
    this._sendBtn.addEventListener("click", () => this._handleSend());
    this._inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._handleSend();
      }
    });
    if (this._newChatBtn) {
      this._newChatBtn.addEventListener("click", () => this._handleNewChat());
    }
  }

  _handleNewChat() {
    if (this._sending || this._restoring) return;
    this.reset();
    if (this._onNewChat) this._onNewChat();
  }

  /**
   * Reset the chat panel: clear messages, re-arm welcome message, allow input.
   * Does NOT call the onNewChat callback; use _handleNewChat for that.
   */
  reset() {
    this._messagesEl.textContent = "";
    this._welcomeShown = false;
    if (this._isOpen && this._welcomeMessage) {
      // Re-inject welcome message so the cleared panel isn't blank
      this._welcomeShown = true;
      const msg = document.createElement("div");
      msg.className = "vektra-chat-msg assistant";
      msg.textContent = this._welcomeMessage;
      this._messagesEl.appendChild(msg);
    }
  }

  /**
   * Replay a list of stored turns as user/assistant message bubbles (WI-5).
   * Call after restoring a conversation from sessionStorage but before
   * enabling user input. Each turn is a plain object with
   * { question, answer, sources }. Sources may be empty for now.
   */
  replayTurns(turns) {
    if (!Array.isArray(turns)) return;
    // Replaying takes ownership of the message list — clear any welcome msg
    // so the history is the first thing the student sees.
    this._messagesEl.textContent = "";
    this._welcomeShown = true; // don't re-emit welcome on first open
    for (const turn of turns) {
      if (turn.question) this.addMessage("user", turn.question);
      if (turn.answer) {
        const msgEl = this.addMessage("assistant", turn.answer);
        if (Array.isArray(turn.sources) && turn.sources.length > 0) {
          this.addSources(msgEl, turn.sources);
        }
      }
    }
  }

  toggle() {
    this._isOpen ? this.close() : this.open();
  }

  open() {
    this._isOpen = true;
    this._panel.classList.add("open");
    // Emit welcome message on first open (WI-4). Rendered as an assistant
    // bubble so it flows naturally with subsequent conversation turns.
    if (this._welcomeMessage && !this._welcomeShown) {
      this._welcomeShown = true;
      const msg = document.createElement("div");
      msg.className = "vektra-chat-msg assistant";
      msg.textContent = this._welcomeMessage;
      this._messagesEl.appendChild(msg);
      this._scrollToBottom();
    }
    this._inputEl.focus();
  }

  close() {
    this._isOpen = false;
    this._panel.classList.remove("open");
  }

  _handleSend() {
    if (this._sending || this._restoring) return;
    const question = this._inputEl.value.trim();
    if (!question) return;

    this._inputEl.value = "";
    this.addMessage("user", question);
    this._setSending(true);

    if (this._onSend) {
      this._onSend(question);
    }
  }

  _setSending(sending) {
    this._sending = sending;
    this._updateControlsDisabled();
  }

  /**
   * Toggle the "restoring history" state (WI-5). While active, the input
   * and send button are disabled so a fast user cannot preempt a pending
   * replay — otherwise `replayTurns` would wipe a user message that was
   * already rendered, and a new_chat click would race the restore.
   */
  setRestoring(restoring) {
    this._restoring = restoring;
    this._updateControlsDisabled();
  }

  _updateControlsDisabled() {
    const blocked =
      this._sending ||
      this._restoring ||
      this._status === "unavailable" ||
      this._status === "sessionExpired";
    this._sendBtn.disabled = blocked;
    this._inputEl.disabled = blocked;
  }

  /**
   * Add a message bubble to the chat.
   * @param {"user"|"assistant"} role
   * @param {string} text
   * @returns {HTMLElement} the message element
   */
  addMessage(role, text) {
    const msg = document.createElement("div");
    msg.className = `vektra-chat-msg ${role}`;
    if (role === "assistant") {
      msg.innerHTML = renderMarkdown(text);
    } else {
      msg.textContent = text;
    }
    this._messagesEl.appendChild(msg);
    this._scrollToBottom();
    return msg;
  }

  /**
   * Create an empty assistant message for streaming tokens.
   * @returns {{ el: HTMLElement, rawText: string }}
   */
  createStreamMessage() {
    const msg = document.createElement("div");
    msg.className = "vektra-chat-msg assistant";
    msg.textContent = "";
    this._messagesEl.appendChild(msg);
    this._scrollToBottom();
    return { el: msg, rawText: "" };
  }

  /**
   * Append a token to a streaming message and re-render markdown.
   * @param {{ el: HTMLElement, rawText: string }} stream
   * @param {string} token
   */
  appendToken(stream, token) {
    stream.rawText += token;
    stream.el.innerHTML = renderMarkdown(stream.rawText);
    this._scrollToBottom();
  }

  /**
   * Add source citations below the last assistant message.
   * @param {HTMLElement|{el: HTMLElement}} msgOrStream - message element or stream object
   * @param {Array} sources
   */
  addSources(msgOrStream, sources) {
    const msgEl = msgOrStream.el || msgOrStream;
    if (!sources || sources.length === 0) return;

    // FEAT-021: link [n] citation markers in the answer to their source.
    // Marker order matches the sources order because the prompt numbers only
    // the sources the response carries: a withheld chunk (FEAT-026) is handed
    // to the model without an id, so it consumes no number. Hence [n] ->
    // sources[n-1]. A marker outside the list is left unlinked rather than
    // guessed at.
    for (const cite of msgEl.querySelectorAll("sup.vektra-cite")) {
      const idx = parseInt(cite.getAttribute("data-cite"), 10) - 1;
      const src = sources[idx];
      const label = src && (src.title || src.document_name);
      if (label) {
        cite.title = label;
        cite.classList.add("linked");
      }
    }

    const container = document.createElement("div");
    container.className = "vektra-chat-sources";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "vektra-chat-sources-toggle";
    toggle.textContent = `${this._lang.sources} (${sources.length})`;
    toggle.setAttribute("aria-expanded", "false");
    container.appendChild(toggle);

    const list = document.createElement("div");
    list.className = "vektra-chat-sources-list";
    ChatUI._sourcesSeq = (ChatUI._sourcesSeq || 0) + 1;
    const listId = `vektra-sources-${ChatUI._sourcesSeq}`;
    list.id = listId;
    toggle.setAttribute("aria-controls", listId);

    for (const [i, src] of sources.entries()) {
      const item = document.createElement("div");
      item.className = "vektra-chat-source-item";
      const score = typeof src.score === "number" ? src.score.toFixed(2) : "?";
      const label = src.document_name || src.chunk_id || this._lang.sourceFallback;
      const rawSnippet = typeof src.snippet === "string" ? src.snippet.trim() : "";
      const maxLen = 120;
      const snippet = rawSnippet.slice(0, maxLen);
      const truncated = rawSnippet.length > maxLen;

      const num = document.createElement("span");
      num.className = "vektra-chat-source-num";
      num.textContent = `[${i + 1}]`;
      item.appendChild(num);

      const text = document.createElement("span");
      text.className = "vektra-chat-source-text";
      text.textContent = `${label} (${score})`;
      item.appendChild(text);

      if (snippet) {
        const snip = document.createElement("div");
        snip.className = "vektra-chat-source-snippet";
        snip.textContent = truncated ? snippet + "\u2026" : snippet;
        item.appendChild(snip);
      }

      list.appendChild(item);
    }

    container.appendChild(list);
    toggle.addEventListener("click", () => {
      const expanded = list.classList.toggle("open");
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    });

    msgEl.appendChild(container);
    this._scrollToBottom();
  }

  /**
   * Show an error message.
   * @param {string} [message]
   */
  showError(message) {
    const msg = document.createElement("div");
    msg.className = "vektra-chat-msg assistant";
    msg.style.color = "#ef4444";
    msg.textContent = message || this._lang.error;
    this._messagesEl.appendChild(msg);
    this._scrollToBottom();
  }

  /**
   * Show or hide a connection status banner at the top of the messages area.
   * @param {"unavailable"|"reconnecting"|"sessionExpired"|null} status - null to clear
   */
  setConnectionStatus(status) {
    // Remove existing banner if any
    const existing = this._panel.querySelector(".vektra-chat-status");
    if (existing) existing.remove();

    this._status = status;

    if (!status) {
      this._updateControlsDisabled();
      return;
    }

    const banner = document.createElement("div");
    banner.className = "vektra-chat-status";
    banner.textContent = this._lang[status] || status;
    this._messagesEl.insertBefore(banner, this._messagesEl.firstChild);

    this._updateControlsDisabled();
  }

  /**
   * Return the localized "no relevant context" message.
   * @returns {string}
   */
  noRelevantContextMessage() {
    return this._lang.noRelevantContext;
  }

  /**
   * Mark sending complete (re-enable input).
   */
  doneSending() {
    this._setSending(false);
    this._inputEl.focus();
  }

  _scrollToBottom() {
    this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
  }
}
