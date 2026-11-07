// The discussion UI controller (plan 9): wires the bridge, the state machine, the API calls,
// the event stream and the views. Tokens live in this closure only (no storage, no URL, no log).
import { Bridge, type InboundMessage } from "./bridge";
import type { PageConfig } from "./main";
import { M } from "./messages.it";
import { consume, type SseEvent } from "./sse";
import { INITIAL, reduce, type Action, type UiState } from "./state";
import { ChatView, type Turn } from "./views/chat";
import { ClosingView, saveBlob } from "./views/closing";
import { ControlsView, copyText, minutesLeft } from "./views/controls";
import { h, LiveRegion } from "./views/dom";

interface SessionView {
  session_id: string;
  state: string;
  close_reason: string | null;
  deadline_at: string | null;
  server_now: string;
  reply_pending: boolean;
  turns: Turn[];
  token?: string;
}

export class App {
  private state: UiState = INITIAL;
  private token: string | null = null; // the session (or pre-session) token, memory only
  private sid: string | null = null;
  private closedAt: Date | null = null;
  private deadline: number | null = null;
  private clockOffset = 0; // server time - local time, ms
  private abort = new AbortController();
  private pendingDownload: string | null = null;
  readonly live = new LiveRegion(M.statusLive);
  readonly status = h("p", { class: "status", id: "status", tabindex: "-1" });
  readonly banner = h("div", { class: "banner", role: "region", "aria-label": M.outage, hidden: true });
  readonly chat: ChatView;
  readonly controls: ControlsView;
  private readonly main: HTMLElement;

  constructor(
    private readonly cfg: PageConfig,
    private readonly bridge: Bridge,
    root: HTMLElement,
    private readonly fetchImpl: typeof fetch = (...a) => fetch(...a),
    maxChars = 2000,
  ) {
    this.chat = new ChatView(
      {
        copy: (turn, button) => {
          const target = button.closest("article") as HTMLElement;
          void copyText(turn.text, target).then((ok) => ok && this.live.say(M.copied));
        },
        flag: (turn, article) => void this.flagTurn(turn, article),
      },
      this.live,
      maxChars,
    );
    this.controls = new ControlsView(
      () => void this.retry(),
      () => void this.finish(),
    );
    this.chat.form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      void this.sendTurn();
    });
    this.main = root;
    root.replaceChildren(
      h("h1", { class: "title", text: M.title }),
      this.status,
      this.banner,
      this.live.el,
    );
    this.setStatus(M.waiting);
    setInterval(() => this.tick(), 15_000);
  }

  start(): void {
    this.bridge.listen((m) => void this.onMessage(m));
    this.bridge.watchHeight(this.main);
    this.bridge.send({ v: 1, type: "vektra-assess-ready" });
  }

  private dispatch(a: Action): void {
    const before = this.state;
    this.state = reduce(this.state, a);
    if (this.state !== before) this.render(before);
  }

  private setStatus(text: string): void {
    this.status.textContent = text;
  }

  private headers(): Record<string, string> {
    return { Authorization: `Bearer ${this.token ?? ""}` };
  }

  private async onMessage(m: InboundMessage): Promise<void> {
    if (m.type === "vektra-assess-unavailable") return this.dispatch({ type: "unavailable" });
    if (m.type === "vektra-assess-download-token") {
      if (m.tid === this.pendingDownload) await this.downloadWith(m.tid, m.token);
      return;
    }
    if (this.state.name !== "awaiting_handoff") return; // one hand-off per page
    this.token = m.token;
    this.dispatch({ type: "handoff", mode: m.mode });
    if (m.mode === "start") await this.startSession();
    else await this.resume(m.sid as string);
  }

  private async startSession(): Promise<void> {
    let r: Response;
    try {
      r = await this.fetchImpl(`${this.cfg.apiBase}/sessions`, {
        method: "POST",
        headers: this.headers(),
      });
    } catch {
      return this.dispatch({ type: "unavailable" });
    }
    if (r.status !== 202) {
      this.token = null;
      return this.dispatch({ type: r.status === 503 ? await this.capacityOr(r) : "unavailable" });
    }
    const body = (await r.json()) as { session_id: string; token: string };
    this.sid = body.session_id;
    this.token = body.token;
    this.dispatch({ type: "start_accepted" });
    this.openStream();
  }

  private async capacityOr(r: Response): Promise<"capacity_refused" | "unavailable"> {
    try {
      const body = (await r.json()) as { error?: { code?: string } };
      return body.error?.code === "ERR-ASSESS-013" ? "capacity_refused" : "unavailable";
    } catch {
      return "unavailable";
    }
  }

  private async resume(sid: string): Promise<void> {
    this.sid = sid;
    const view = await this.fetchSession();
    if (!view) return this.dispatch({ type: "unavailable" });
    if (view.token) this.token = view.token; // resume re-mints the session token
    this.apply(view);
    if (this.state.name !== "closed") this.openStream();
  }

  private async fetchSession(): Promise<SessionView | null> {
    try {
      const r = await this.fetchImpl(`${this.cfg.apiBase}/sessions/${this.sid}`, {
        headers: this.headers(),
        cache: "no-store",
      });
      if (r.status === 409) {
        this.dispatch({ type: "closed", reason: null });
        return null;
      }
      if (!r.ok) return null;
      const date = r.headers.get("Date");
      const view = (await r.json()) as SessionView;
      const serverNow = Date.parse(view.server_now || date || "");
      if (!Number.isNaN(serverNow)) this.clockOffset = serverNow - Date.now();
      return view;
    } catch {
      return null;
    }
  }

  private apply(view: SessionView): void {
    this.deadline = view.deadline_at ? Date.parse(view.deadline_at) : null;
    this.dispatch({ type: "session_state", state: view.state, closeReason: view.close_reason });
    if (view.state !== "STARTING") this.chat.render(view.turns);
    this.chat.setEnabled(
      this.state.name === "active" && !view.reply_pending,
      view.reply_pending ? M.replyPending : "",
    );
    this.tick();
  }

  private openStream(): void {
    void consume({
      url: `${this.cfg.apiBase}/sessions/${this.sid}/events`,
      token: () => this.token,
      onEvent: (ev) => void this.onEvent(ev),
      onTerminal: (status) =>
        this.dispatch(status === 409 ? { type: "closed", reason: null } : { type: "unavailable" }),
      fetchImpl: this.fetchImpl,
      signal: this.abort.signal,
    });
  }

  private async onEvent(ev: SseEvent): Promise<void> {
    let data: Record<string, unknown> = {};
    try {
      data = ev.data ? (JSON.parse(ev.data) as Record<string, unknown>) : {};
    } catch {
      return;
    }
    switch (ev.event) {
      case "committed": {
        const view = await this.fetchSession();
        if (view) this.apply(view);
        this.dispatch({ type: "committed" });
        return;
      }
      case "start_rolled_back":
        return this.dispatch({ type: "rolled_back", reason: String(data.reason ?? "") });
      case "reply_delta":
        if (typeof data.text === "string") this.chat.delta(data.text);
        return;
      case "reply_done":
      case "reply_interrupted": {
        // the persisted turn replaces the streamed text: the DOM shows exactly what was stored
        const view = await this.fetchSession();
        if (view) this.apply(view);
        this.chat.replyDone(ev.event === "reply_interrupted");
        return;
      }
      case "warning":
        if (typeof data.minutes === "number") this.live.say(M.warning(data.minutes));
        return;
      case "budget_warning":
        return;
      case "outage_start":
        this.banner.replaceChildren(h("p", { text: M.outage }));
        this.banner.hidden = false;
        return this.dispatch({ type: "outage_start" });
      case "outage_end":
        this.banner.replaceChildren(h("p", { text: M.outageEnd }));
        if (typeof data.deadline_at === "string") this.deadline = Date.parse(data.deadline_at);
        this.dispatch({ type: "outage_end" });
        setTimeout(() => (this.banner.hidden = true), 8000);
        return;
      case "closed":
        this.closedAt = new Date(Date.now() + this.clockOffset);
        return this.dispatch({ type: "closed", reason: data.close_reason as string | null });
      default:
        return;
    }
  }

  private tick(): void {
    // the time left counts down while ACTIVE only; in OUTAGE the last value stays (frozen)
    if (this.state.name === "active" && this.deadline !== null)
      this.controls.setTime(minutesLeft(this.deadline, Date.now() + this.clockOffset));
    else if (this.state.name !== "outage") this.controls.setTime(null);
  }

  private async sendTurn(): Promise<void> {
    const text = this.chat.composer.value;
    if (!text.trim() || this.state.name !== "active") return;
    this.chat.setEnabled(false, "");
    let r: Response;
    try {
      r = await this.fetchImpl(`${this.cfg.apiBase}/sessions/${this.sid}/turns`, {
        method: "POST",
        headers: { ...this.headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    } catch {
      return this.chat.setEnabled(true, M.sendFailed);
    }
    if (r.status === 202) {
      this.chat.composer.value = "";
      const body = (await r.json()) as { turn_no: number };
      this.chat.add({ turn_no: body.turn_no, role: "student", kind: "normal", text });
      return this.chat.setEnabled(false, M.replyPending);
    }
    const help =
      r.status === 429 ? M.waitAFewSeconds : r.status === 409 ? M.replyPending : M.sendFailed;
    this.chat.setEnabled(r.status !== 409, help);
  }

  private async retry(): Promise<void> {
    try {
      await this.fetchImpl(`${this.cfg.apiBase}/sessions/${this.sid}/retry`, {
        method: "POST",
        headers: this.headers(),
      });
    } catch {
      // the server's own probe still runs; nothing changes here
    }
  }

  private async finish(): Promise<void> {
    try {
      const r = await this.fetchImpl(`${this.cfg.apiBase}/sessions/${this.sid}/finish`, {
        method: "POST",
        headers: this.headers(),
      });
      if (r.ok) {
        const body = (await r.json()) as { close_reason: string };
        this.closedAt = new Date(Date.now() + this.clockOffset);
        this.dispatch({ type: "closed", reason: body.close_reason });
      }
    } catch {
      this.chat.setEnabled(true, M.sendFailed);
    }
  }

  private async flagTurn(turn: Turn, article: HTMLElement): Promise<void> {
    if (this.state.name !== "active" && this.state.name !== "outage") return;
    if (!window.confirm(M.flagConfirm)) return;
    const r = await this.fetchImpl(
      `${this.cfg.apiBase}/sessions/${this.sid}/turns/${turn.turn_no}/flag`,
      { method: "POST", headers: this.headers() },
    );
    if (r.ok) {
      article.querySelector("button.flag")?.replaceWith(h("span", { class: "flag-marker", text: M.flagged }));
    }
  }

  private requestDownload(): void {
    if (!this.sid) return;
    this.pendingDownload = this.sid;
    this.bridge.send({ v: 1, type: "vektra-assess-download-token-request", tid: this.sid });
  }

  private async downloadWith(tid: string, token: string): Promise<void> {
    this.pendingDownload = null;
    const closing = this.main.querySelector(".download-status");
    try {
      const r = await this.fetchImpl(`${this.cfg.apiBase}/transcripts/${tid}`, {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      if (!r.ok) throw new Error(String(r.status));
      saveBlob(await r.blob(), "trascrizione.html");
    } catch {
      if (closing) closing.textContent = M.downloadFailed;
    }
  }

  private render(before: UiState): void {
    const s = this.state;
    const keep = [this.main.querySelector("h1"), this.status, this.banner, this.live.el];
    switch (s.name) {
      case "preparing":
        this.setStatus(M.preparing);
        if (s.announced && !(before.name === "preparing" && before.announced))
          this.live.say(M.preparing);
        return;
      case "resuming":
        return this.setStatus(M.resuming);
      case "active":
      case "outage":
        this.setStatus("");
        if (!this.main.contains(this.chat.root))
          this.main.replaceChildren(...(keep as Node[]), this.controls.root, this.chat.root);
        this.controls.setOutage(s.name === "outage");
        this.chat.setEnabled(s.name === "active", "");
        if (s.name === "active") this.banner.hidden = this.banner.textContent !== M.outageEnd;
        this.tick();
        return;
      case "closed": {
        this.abort.abort();
        this.token = null;
        this.controls.disable();
        this.chat.setEnabled(false, "");
        const closing = new ClosingView(s.reason, this.closedAt, () => this.requestDownload());
        this.banner.hidden = true;
        this.main.replaceChildren(...(keep as Node[]), closing.root, this.chat.root);
        this.chat.list.querySelectorAll("button.flag").forEach((b) => b.remove());
        this.bridge.send({ v: 1, type: "vektra-assess-closed", reason: s.reason });
        closing.focus();
        return;
      }
      case "capacity_refused":
        this.token = null;
        return this.setStatus(M.capacityRefused);
      case "start_rolled_back":
        this.token = null;
        return this.setStatus(s.reason === "technical_problem_persistent" ? M.persistent : M.rolledBack);
      case "unavailable":
        this.abort.abort();
        this.token = null;
        return this.setStatus(M.unavailable);
      default:
        return;
    }
  }
}
