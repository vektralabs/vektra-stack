// postMessage contract v1 with the plugin (docs/reference/api.md, "Assess UI"). A message is
// acted on only when it comes from the configured Moodle origin AND from the parent window,
// with v === 1 and a known type; everything else is ignored. Messages are posted with the
// explicit Moodle origin as targetOrigin, never "*".

export type InboundMessage =
  | { v: 1; type: "vektra-assess-token"; token: string; mode: "start" | "resume"; sid?: string }
  | { v: 1; type: "vektra-assess-unavailable" }
  | { v: 1; type: "vektra-assess-download-token"; tid: string; token: string };

export type OutboundMessage =
  | { v: 1; type: "vektra-assess-ready" }
  | { v: 1; type: "vektra-assess-resize"; height: number }
  | { v: 1; type: "vektra-assess-download-token-request"; tid: string }
  | { v: 1; type: "vektra-assess-closed"; reason: string };

/** Validate the shape of an inbound message; null when it is not one of ours. */
export function parseInbound(data: unknown): InboundMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const m = data as Record<string, unknown>;
  if (m.v !== 1 || typeof m.type !== "string") return null;
  switch (m.type) {
    case "vektra-assess-token":
      if (typeof m.token !== "string" || !m.token) return null;
      if (m.mode !== "start" && m.mode !== "resume") return null;
      if (m.mode === "resume" && typeof m.sid !== "string") return null;
      return m.mode === "resume"
        ? { v: 1, type: m.type, token: m.token, mode: m.mode, sid: m.sid as string }
        : { v: 1, type: m.type, token: m.token, mode: m.mode };
    case "vektra-assess-unavailable":
      return { v: 1, type: m.type };
    case "vektra-assess-download-token":
      if (typeof m.tid !== "string" || typeof m.token !== "string") return null;
      return { v: 1, type: m.type, tid: m.tid, token: m.token };
    default:
      return null;
  }
}

export interface BridgeWindow {
  parent: { postMessage(message: unknown, targetOrigin: string): void };
  addEventListener(type: "message", listener: (ev: MessageEvent) => void): void;
}

export class Bridge {
  constructor(
    private readonly win: BridgeWindow,
    private readonly moodleOrigin: string,
  ) {
    if (!moodleOrigin || moodleOrigin === "*") throw new Error("an explicit origin is required");
  }

  /** Listen for the parent's messages; ``onMessage`` sees validated messages only. */
  listen(onMessage: (m: InboundMessage) => void): void {
    this.win.addEventListener("message", (ev: MessageEvent) => {
      if (ev.origin !== this.moodleOrigin) return;
      if (ev.source !== (this.win.parent as unknown)) return;
      const m = parseInbound(ev.data);
      if (m) onMessage(m);
    });
  }

  send(message: OutboundMessage): void {
    this.win.parent.postMessage(message, this.moodleOrigin);
  }

  /** Report the content height to the parent whenever it changes. */
  watchHeight(el: Element, RO: typeof ResizeObserver = ResizeObserver): void {
    let last = -1;
    new RO(() => {
      const height = Math.ceil(el.getBoundingClientRect().height);
      if (height !== last) {
        last = height;
        this.send({ v: 1, type: "vektra-assess-resize", height });
      }
    }).observe(el);
  }
}
