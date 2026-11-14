// Server-sent events over fetch() (plan 9, ADR-0030 amendment): EventSource cannot send the
// Authorization header, so the event stream is read through a ReadableStream and parsed here.
// The parser is incremental: an event may arrive split across any chunk boundary, lines end in
// LF, CRLF or CR, "data:" may span several lines, ":" lines are heartbeats.

export interface SseEvent {
  event: string;
  data: string;
  id?: string;
}

export class SseParser {
  private buf = "";
  private pendingCR = false;
  private event = "";
  private data: string[] = [];
  private id: string | undefined;
  lastEventId: string | undefined;
  retry: number | undefined;

  /** Feed a decoded chunk; returns the events it completed. */
  push(chunk: string): SseEvent[] {
    let text = chunk;
    if (this.pendingCR) {
      if (text.startsWith("\n")) text = text.slice(1); // the LF of a CRLF split in two
      this.pendingCR = false;
    }
    this.buf += text;
    const out: SseEvent[] = [];
    for (;;) {
      const m = /\r\n|\n|\r/.exec(this.buf);
      if (!m) break;
      if (m[0] === "\r" && m.index === this.buf.length - 1) {
        // a CR at the very end may be the first half of a CRLF: wait for the next chunk
        this.pendingCR = true;
      }
      const line = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  private line(line: string): SseEvent | null {
    if (line === "") {
      if (!this.data.length && !this.event) return null;
      const ev: SseEvent = { event: this.event || "message", data: this.data.join("\n") };
      if (this.id !== undefined) ev.id = this.id;
      this.event = "";
      this.data = [];
      return ev;
    }
    if (line.startsWith(":")) return null; // heartbeat / comment
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    else if (field === "id" && !value.includes("\0")) {
      this.id = value;
      this.lastEventId = value;
    } else if (field === "retry" && /^\d+$/.test(value)) this.retry = Number(value);
    return null;
  }
}

/** Statuses after which reconnecting cannot help: the session is closed or the token is gone. */
export const TERMINAL_STATUSES = new Set([401, 403, 404, 409]);

export interface StreamOptions {
  url: string;
  token: () => string | null;
  onEvent: (ev: SseEvent) => void;
  onTerminal: (status: number) => void;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
  signal?: AbortSignal;
}

/** Backoff before reconnect attempt n (0-based): 1, 2, 4, 8 s cap, with +-20% jitter. */
export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(8000, 1000 * 2 ** attempt);
  return Math.round(base * (0.8 + 0.4 * random()));
}

/** Read the event stream until a terminal status or an abort; reconnects with Last-Event-ID. */
export async function consume(opts: StreamOptions): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const sleep = opts.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  let lastId: string | undefined;
  let attempt = 0;
  while (!opts.signal?.aborted) {
    const token = opts.token();
    if (!token) return opts.onTerminal(401);
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      Accept: "text/event-stream",
    };
    if (lastId) headers["Last-Event-ID"] = lastId;
    let resp: Response;
    try {
      resp = await fetchImpl(opts.url, { headers, signal: opts.signal, cache: "no-store" });
    } catch {
      if (opts.signal?.aborted) return;
      await sleep(backoffMs(attempt++, opts.random));
      continue;
    }
    if (TERMINAL_STATUSES.has(resp.status)) return opts.onTerminal(resp.status);
    if (!resp.ok || !resp.body) {
      await sleep(backoffMs(attempt++, opts.random));
      continue;
    }
    attempt = 0;
    const parser = new SseParser();
    const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
    let ended = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const ev of parser.push(value)) {
          if (ev.id) lastId = ev.id;
          opts.onEvent(ev);
          if (ev.event === "closed" || ev.event === "start_rolled_back") ended = true;
        }
        // a terminal event ends the read even if the server keeps the response open
        if (ended) break;
      }
    } catch {
      // a broken connection: reconnect below
    }
    if (ended) {
      await reader.cancel().catch(() => {});
      return;
    }
    if (ended) return;
    if (opts.signal?.aborted) return;
    await sleep(backoffMs(attempt++, opts.random));
  }
}
