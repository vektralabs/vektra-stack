/**
 * Chat UI: floating button, expandable panel, message list, input.
 * Vanilla DOM manipulation, no framework dependencies.
 */

import { buildStyles } from "./styles.js";

const I18N = {
  en: {
    title: "Course Assistant",
    placeholder: "Ask a question...",
    send: "Send",
    sources: "Sources:",
    error: "An error occurred. Please try again.",
    close: "Close",
  },
  it: {
    title: "Assistente del corso",
    placeholder: "Fai una domanda...",
    send: "Invia",
    sources: "Fonti:",
    error: "Si è verificato un errore. Riprova.",
    close: "Chiudi",
  },
};

export class ChatUI {
  /**
   * @param {object} opts
   * @param {string} opts.theme - "light" or "dark"
   * @param {string} opts.language - "en" or "it"
   * @param {function} opts.onSend - callback(question: string)
   */
  constructor({ theme = "light", language = "en", onSend }) {
    this._theme = theme;
    this._lang = I18N[language] || I18N.en;
    this._onSend = onSend;
    this._isOpen = false;
    this._sending = false;

    this._injectStyles();
    this._createElements();
    this._bindEvents();
  }

  _injectStyles() {
    const style = document.createElement("style");
    style.textContent = buildStyles(this._theme);
    document.head.appendChild(style);
  }

  _createElements() {
    // Floating button
    this._btn = document.createElement("button");
    this._btn.className = "vektra-chat-btn";
    this._btn.setAttribute("aria-label", this._lang.title);
    this._btn.innerHTML = "&#128172;"; // speech bubble emoji
    document.body.appendChild(this._btn);

    // Chat panel
    this._panel = document.createElement("div");
    this._panel.className = "vektra-chat-panel";
    this._panel.innerHTML = `
      <div class="vektra-chat-header">
        <span class="vektra-chat-header-title">${this._lang.title}</span>
        <button class="vektra-chat-close" aria-label="${this._lang.close}">&times;</button>
      </div>
      <div class="vektra-chat-messages"></div>
      <div class="vektra-chat-input-area">
        <input class="vektra-chat-input" type="text"
               placeholder="${this._lang.placeholder}"
               aria-label="${this._lang.placeholder}" />
        <button class="vektra-chat-send">${this._lang.send}</button>
      </div>
    `;
    document.body.appendChild(this._panel);

    // Cache references
    this._messagesEl = this._panel.querySelector(".vektra-chat-messages");
    this._inputEl = this._panel.querySelector(".vektra-chat-input");
    this._sendBtn = this._panel.querySelector(".vektra-chat-send");
    this._closeBtn = this._panel.querySelector(".vektra-chat-close");
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
  }

  toggle() {
    this._isOpen ? this.close() : this.open();
  }

  open() {
    this._isOpen = true;
    this._panel.classList.add("open");
    this._inputEl.focus();
  }

  close() {
    this._isOpen = false;
    this._panel.classList.remove("open");
  }

  _handleSend() {
    if (this._sending) return;
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
    this._sendBtn.disabled = sending;
    this._inputEl.disabled = sending;
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
    msg.textContent = text;
    this._messagesEl.appendChild(msg);
    this._scrollToBottom();
    return msg;
  }

  /**
   * Create an empty assistant message for streaming tokens.
   * @returns {HTMLElement}
   */
  createStreamMessage() {
    const msg = document.createElement("div");
    msg.className = "vektra-chat-msg assistant";
    msg.textContent = "";
    this._messagesEl.appendChild(msg);
    this._scrollToBottom();
    return msg;
  }

  /**
   * Append a token to a streaming message element.
   * @param {HTMLElement} msgEl
   * @param {string} token
   */
  appendToken(msgEl, token) {
    msgEl.textContent += token;
    this._scrollToBottom();
  }

  /**
   * Add source citations below the last assistant message.
   * @param {HTMLElement} msgEl
   * @param {Array} sources
   */
  addSources(msgEl, sources) {
    if (!sources || sources.length === 0) return;

    const container = document.createElement("div");
    container.className = "vektra-chat-sources";
    container.innerHTML = `<strong>${this._lang.sources}</strong>`;

    for (const src of sources) {
      const item = document.createElement("div");
      item.className = "vektra-chat-source-item";
      const score = typeof src.score === "number" ? src.score.toFixed(2) : "?";
      item.textContent = `${src.snippet || src.chunk_id || "Source"} (${score})`;
      container.appendChild(item);
    }

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
