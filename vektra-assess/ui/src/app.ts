// The discussion UI controller (plan 9): wires the bridge, the state machine, the API calls,
// the event stream and the views. Tokens live in this closure only (no storage, no URL, no log).
import { Bridge, type InboundMessage } from "./bridge";
import type { PageConfig } from "./main";
import { M } from "./messages.it";
import { consume, type SseEvent } from "./sse";
import { INITIAL, reduce, type Action, type UiState } from "./state";
import { ChatView, type Turn } from "./views/chat";
import { ClosingView, saveBlob } from "./views/closing";
import { ConfirmDialog, ControlsView, copyText, minutesLeft } from "./views/controls";
import { h, LiveRegion } from "./views/dom";

/** How long the closing screen waits for the parent's download token. */
export const DOWNLOAD_TIMEOUT_MS = 10_000;

interface SessionView {
  session_id: string;
  state: string;
  close_reason: string | null;
  deadline_at: string | null;
  server_now: string;
  reply_pending: boolean;
  remaining_s?: number; // frozen at the outage start while in OUTAGE
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
  private downloadTimer: ReturnType<typeof setTimeout> | undefined; // the pending request's
  private bannerTimer: ReturnType<typeof setTimeout> | undefined; // hides the recovery text
  private replyPending = false; // the server owes a reply to the last student turn
  private readsIssued = 0; // GET /sessions/{sid} reads started, in order
  private readApplied = 0; // the newest read applied to the screen
  private lastTurnNo = 0; // the highest turn number on screen from a snapshot
  /** Waits between the attempts to read the session once it is committed (tests shorten it). */
  snapshotRetryMs = [1000, 3000];
  readonly live = new LiveRegion(M.statusLive);
  readonly status = h("p", { class: "status", id: "status", tabindex: "-1" });
  readonly banner = h("div", { class: "banner", role: "region", "aria-label": M.outage, hidden: true });
  readonly chat: ChatView;
  readonly controls: ControlsView;
  readonly flagDialog = new ConfirmDialog(
    "flag-confirm",
    M.flagConfirm,
    M.flagConfirmText,
    M.flagYes,
    M.confirmNo,
  );
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
    this.chat.send.addEventListener("click", () => void this.sendTurn());
    this.main = root;
    root.replaceChildren(
      h("h1", { class: "title", text: M.title }),
      this.status,
      this.banner,
      this.live.el,
      this.flagDialog.dialog,
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
    else await this.resume(m.sid ?? (await this.openSessionId()));
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

  /** The session is committed: show it only once its snapshot (opening, deadline, turns) is
   *  loaded, retrying a failed read; if every attempt fails the discussion is unavailable
   *  (reloading resumes it), never an empty open conversation (4501067234). */
  private async loadCommitted(): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      if (await this.refresh()) return this.dispatch({ type: "committed" });
      if (this.state.name === "closed") return;
      if (attempt >= this.snapshotRetryMs.length) return this.dispatch({ type: "unavailable" });
      await new Promise((r) => setTimeout(r, this.snapshotRetryMs[attempt]));
    }
  }

  private async capacityOr(r: Response): Promise<"capacity_refused" | "unavailable"> {
    try {
      const body = (await r.json()) as { error?: { code?: string } };
      return body.error?.code === "ERR-ASSESS-013" ? "capacity_refused" : "unavailable";
    } catch {
      return "unavailable";
    }
  }

  /** The student's open session, looked up with the pre-session token (GET /state). */
  private async openSessionId(): Promise<string | null> {
    try {
      const r = await this.fetchImpl(`${this.cfg.apiBase}/state`, {
        headers: this.headers(),
        cache: "no-store",
      });
      if (!r.ok) return null;
      const body = (await r.json()) as { session_id?: string };
      return typeof body.session_id === "string" ? body.session_id : null;
    } catch {
      return null;
    }
  }

  private async resume(sid: string | null): Promise<void> {
    if (!sid) return this.dispatch({ type: "unavailable" });
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

  /** Read the session and apply it, unless a newer read was applied meanwhile or the UI is
   *  closed: overlapping refreshes never put an older transcript back on screen. */
  private async refresh(): Promise<SessionView | null> {
    const seq = ++this.readsIssued;
    const view = await this.fetchSession();
    if (!view || seq < this.readApplied || this.state.name === "closed") return null;
    this.readApplied = seq;
    this.apply(view);
    return view;
  }

  private apply(view: SessionView): void {
    if (this.state.name === "closed") return; // a late snapshot never reopens the screen
    this.lastTurnNo = Math.max(0, ...view.turns.map((t) => t.turn_no));
    this.deadline = view.deadline_at ? Date.parse(view.deadline_at) : null;
    this.dispatch({ type: "session_state", state: view.state, closeReason: view.close_reason });
    if (view.state !== "STARTING") this.chat.render(view.turns);
    this.replyPending = view.reply_pending;
    if (view.state === "OUTAGE") {
      // a resume into an outage gets no outage_start replayed: the snapshot says it, and the
      // frozen time left comes from it (4501070038, 4501070048)
      this.showOutage();
      if (typeof view.remaining_s === "number")
        this.controls.setTime(minutesLeft(view.remaining_s * 1000, 0));
    }
    this.syncComposer();
    this.tick();
  }

  private showOutage(): void {
    clearTimeout(this.bannerTimer); // a recovery's pending hide never hides a new outage
    this.banner.replaceChildren(h("p", { text: M.outage }));
    this.banner.hidden = false;
  }

  /** The one rule for the composer: open only while ACTIVE and no reply is owed. */
  private syncComposer(): void {
    this.chat.setEnabled(
      this.state.name === "active" && !this.replyPending,
      this.replyPending ? M.replyPending : "",
    );
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
      case "committed":
        return this.loadCommitted();
      case "state":
        // the stream's first event is a state snapshot: a session that committed before the
        // stream opened sends ACTIVE (or OUTAGE) and no committed event (4501070029)
        if (this.state.name === "preparing" && (data.state === "ACTIVE" || data.state === "OUTAGE"))
          return this.loadCommitted();
        return;
      case "start_rolled_back":
        return this.dispatch({ type: "rolled_back", reason: String(data.reason ?? "") });
      case "reply_delta":
        if (typeof data.text === "string") this.chat.delta(data.text);
        return;
      case "reply_done":
      case "reply_interrupted": {
        // the persisted turn replaces the streamed text: the DOM shows exactly what was stored
        await this.refresh();
        this.chat.replyDone(ev.event === "reply_interrupted");
        return;
      }
      case "warning":
        if (typeof data.minutes === "number") this.live.say(M.warning(data.minutes));
        return;
      case "budget_warning":
        return;
      case "outage_start":
        this.showOutage();
        this.dispatch({ type: "outage_start" });
        if (this.state.name === "outage") this.live.say(M.outageLive); // why, and Riprova
        return;
      case "outage_end":
        this.banner.replaceChildren(h("p", { text: M.outageEnd }));
        if (typeof data.deadline_at === "string") this.deadline = Date.parse(data.deadline_at);
        this.dispatch({ type: "outage_end" });
        clearTimeout(this.bannerTimer);
        this.bannerTimer = setTimeout(() => (this.banner.hidden = true), 8000);
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
      return this.sendFailed(M.sendFailed);
    }
    if (r.status === 202) {
      this.chat.composer.value = "";
      const body = (await r.json()) as { turn_no: number };
      // a snapshot applied meanwhile may already hold this turn (and its reply): it is the
      // server's word, so nothing is added and no reply is assumed owed
      if (body.turn_no > this.lastTurnNo) {
        this.chat.add({ turn_no: body.turn_no, role: "student", kind: "normal", text });
        this.lastTurnNo = body.turn_no;
        this.replyPending = true;
      }
      return this.syncComposer();
    }
    if (r.status === 409) return this.chat.setEnabled(false, M.replyPending);
    this.sendFailed(r.status === 429 ? M.waitAFewSeconds : M.sendFailed);
  }

  /** A turn that did not go through: the composer opens again only if the session is still
   *  ACTIVE with no reply owed (an outage or a close meanwhile keeps it closed, 4501070043). */
  private sendFailed(help: string): void {
    if (this.state.name === "active" && !this.replyPending) this.chat.setEnabled(true, help);
    else this.syncComposer();
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
        return;
      }
    } catch {
      // the network lost it: the server may or may not have closed, so ask it below
    }
    // not concluded (or unknown): re-read the session so the screen shows the server's state
    // (still active, or closed meanwhile), then say so
    await this.refresh();
    if (this.state.name !== "closed") {
      this.setStatus(M.finishFailed);
      this.live.say(M.finishFailed);
    }
  }

  private async flagTurn(turn: Turn, article: HTMLElement): Promise<void> {
    if (this.state.name !== "active" && this.state.name !== "outage") return;
    const button = article.querySelector("button.flag") as HTMLButtonElement | null;
    if (!button || !(await this.flagDialog.ask(button))) return;
    if (this.state.name !== "active" && this.state.name !== "outage") return;
    article.querySelector(".flag-error")?.remove();
    let r: Response | null = null;
    try {
      r = await this.fetchImpl(
        `${this.cfg.apiBase}/sessions/${this.sid}/turns/${turn.turn_no}/flag`,
        { method: "POST", headers: this.headers() },
      );
    } catch {
      r = null;
    }
    if (!r || !r.ok) {
      // the control stays: the student may try again; the text is announced once
      button.after(h("p", { class: "flag-error", role: "status", text: M.flagFailed }));
      return;
    }
    const marker = h("span", { class: "flag-marker", tabindex: "-1", text: M.flagged });
    button.replaceWith(marker);
    marker.focus();
  }

  /** Ask the parent for a download token; one request at a time, no automatic retry. If the
   *  parent does not answer within DOWNLOAD_TIMEOUT_MS (a plugin without the download part),
   *  a generic text is shown and the button works again. */
  private requestDownload(): void {
    if (!this.sid || this.pendingDownload) return;
    const tid = this.sid;
    this.pendingDownload = tid;
    const button = this.main.querySelector(".closing button") as HTMLButtonElement | null;
    if (button) button.disabled = true;
    this.bridge.send({ v: 1, type: "vektra-assess-download-token-request", tid });
    clearTimeout(this.downloadTimer);
    this.downloadTimer = setTimeout(() => {
      if (this.pendingDownload !== tid) return;
      this.pendingDownload = null;
      const status = this.main.querySelector(".download-status");
      if (status) status.textContent = M.downloadFailed;
      if (button) button.disabled = false;
    }, DOWNLOAD_TIMEOUT_MS);
  }

  private async downloadWith(tid: string, token: string): Promise<void> {
    clearTimeout(this.downloadTimer); // answered: this request's deadline is gone (4501067245)
    this.pendingDownload = null;
    const button = this.main.querySelector(".closing button") as HTMLButtonElement | null;
    if (button) button.disabled = false;
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
    const keep = [
      this.main.querySelector("h1"),
      this.status,
      this.banner,
      this.live.el,
      this.flagDialog.dialog,
    ];
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
        this.syncComposer(); // never opens over a reply the server still owes (review #5)
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
        // nothing can be sent any more: no composer, no Fine, no dialog left open over it
        this.controls.disable();
        if (this.controls.dialog.open) this.controls.dialog.close("cancel");
        if (this.flagDialog.dialog.open) this.flagDialog.dialog.close("cancel");
        this.chat.setEnabled(false, "");
        this.live.say(M.unavailable);
        return this.setStatus(M.unavailable);
      default:
        return;
    }
  }
}
