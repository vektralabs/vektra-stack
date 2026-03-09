/**
 * CSS-in-JS styles for the Vektra chatbot widget.
 * Injected as a <style> tag to avoid external CSS dependencies.
 * Supports light and dark themes.
 */

const THEMES = {
  light: {
    bg: "#ffffff",
    bgSecondary: "#f7f7f8",
    text: "#1a1a1a",
    textSecondary: "#6b7280",
    border: "#e5e7eb",
    primary: "#2563eb",
    primaryHover: "#1d4ed8",
    userBubble: "#2563eb",
    userText: "#ffffff",
    assistantBubble: "#f3f4f6",
    assistantText: "#1a1a1a",
    inputBg: "#ffffff",
    shadow: "0 4px 24px rgba(0, 0, 0, 0.12)",
  },
  dark: {
    bg: "#1e1e2e",
    bgSecondary: "#2a2a3e",
    text: "#e4e4e7",
    textSecondary: "#a1a1aa",
    border: "#3f3f5e",
    primary: "#3b82f6",
    primaryHover: "#2563eb",
    userBubble: "#3b82f6",
    userText: "#ffffff",
    assistantBubble: "#2a2a3e",
    assistantText: "#e4e4e7",
    inputBg: "#2a2a3e",
    shadow: "0 4px 24px rgba(0, 0, 0, 0.4)",
  },
};

export function getThemeVars(theme) {
  return THEMES[theme] || THEMES.light;
}

export function buildStyles(theme) {
  const t = getThemeVars(theme);

  return `
.vektra-chat-btn {
  position: fixed;
  bottom: 24px;
  right: 24px;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: ${t.primary};
  color: #fff;
  border: none;
  cursor: pointer;
  box-shadow: ${t.shadow};
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  transition: background 0.2s, transform 0.2s;
  font-size: 24px;
  line-height: 1;
}
.vektra-chat-btn:hover {
  background: ${t.primaryHover};
  transform: scale(1.05);
}

.vektra-chat-panel {
  position: fixed;
  bottom: 96px;
  right: 24px;
  width: 400px;
  height: 500px;
  max-width: calc(100vw - 48px);
  max-height: calc(100vh - 120px);
  background: ${t.bg};
  border: 1px solid ${t.border};
  border-radius: 12px;
  box-shadow: ${t.shadow};
  display: none;
  flex-direction: column;
  z-index: 10001;
  overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  color: ${t.text};
}
.vektra-chat-panel.open {
  display: flex;
}

.vektra-chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid ${t.border};
  background: ${t.bgSecondary};
  flex-shrink: 0;
}
.vektra-chat-header-title {
  font-weight: 600;
  font-size: 15px;
}
.vektra-chat-close {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 18px;
  color: ${t.textSecondary};
  padding: 4px;
  line-height: 1;
}

.vektra-chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vektra-chat-msg {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.5;
  word-wrap: break-word;
}
.vektra-chat-msg.user {
  align-self: flex-end;
  background: ${t.userBubble};
  color: ${t.userText};
  border-bottom-right-radius: 4px;
}
.vektra-chat-msg.assistant {
  align-self: flex-start;
  background: ${t.assistantBubble};
  color: ${t.assistantText};
  border-bottom-left-radius: 4px;
}

.vektra-chat-sources {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid ${t.border};
  font-size: 12px;
  color: ${t.textSecondary};
}
.vektra-chat-source-item {
  margin-top: 4px;
}

.vektra-chat-input-area {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  border-top: 1px solid ${t.border};
  background: ${t.bgSecondary};
  gap: 8px;
  flex-shrink: 0;
}
.vektra-chat-input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid ${t.border};
  border-radius: 8px;
  background: ${t.inputBg};
  color: ${t.text};
  font-size: 14px;
  outline: none;
  font-family: inherit;
}
.vektra-chat-input:focus-visible {
  outline: 2px solid ${t.primary};
  outline-offset: -1px;
}
.vektra-chat-input::placeholder {
  color: ${t.textSecondary};
}
.vektra-chat-send {
  background: ${t.primary};
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.2s;
}
.vektra-chat-send:hover {
  background: ${t.primaryHover};
}
.vektra-chat-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 480px) {
  .vektra-chat-panel {
    width: calc(100vw - 16px);
    height: calc(100vh - 80px);
    bottom: 8px;
    right: 8px;
    border-radius: 8px;
  }
  .vektra-chat-btn {
    bottom: 16px;
    right: 16px;
  }
}
`;
}
