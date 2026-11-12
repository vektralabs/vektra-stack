// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { App, DOWNLOAD_TIMEOUT_MS, transcriptFileName } from "../src/app";
import { Bridge } from "../src/bridge";
import { readConfig } from "../src/main";

function app() {
  const posted: unknown[] = [];
  const win = {
    parent: { postMessage: (m: unknown) => posted.push(m) },
    addEventListener: () => {},
  };
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const a = new App(
    { site: "s", parentOrigin: "https://m.example", apiBase: "/api/v1/assess" },
    new Bridge(win, "https://m.example"),
    root,
    (async () => new Response("{}", { status: 200 })) as unknown as typeof fetch,
  );
  return { a, root, posted };
}

describe("warnings and outage", () => {
  it("announces a warning once, in the polite live region, with no code", async () => {
    const { a } = app();
    const ev = (a as unknown as { onEvent: (e: unknown) => Promise<void> }).onEvent.bind(a);
    await ev({ event: "warning", data: '{"minutes":5,"deadline_at":"x"}' });
    expect(a.live.announcements).toBe(1);
    expect(a.live.el.textContent).toBe("Attenzione: mancano 5 minuti alla fine");
    expect(a.live.el.getAttribute("aria-live")).toBe("polite");
  });

  it("shows the outage banner with the credited-time text and no technical detail", async () => {
    const { a } = app();
    (a as unknown as { state: unknown }).state = { name: "active" };
    const ev = (a as unknown as { onEvent: (e: unknown) => Promise<void> }).onEvent.bind(a);
    await ev({ event: "outage_start", data: '{"state":"OUTAGE","reason":"provider_unavailable"}' });
    expect(a.banner.hidden).toBe(false);
    expect(a.banner.textContent).toBe(
      "Assistente temporaneamente non disponibile: questo tempo non ti viene conteggiato.",
    );
    expect(a.banner.textContent).not.toContain("provider");
    expect(a.controls.retry.hidden).toBe(false);
    await ev({ event: "outage_end", data: '{"state":"ACTIVE"}' });
    expect(a.banner.textContent).toContain("ti è stato restituito");
    expect(a.controls.retry.hidden).toBe(true);
  });

  it("tells the parent when the session is closed", async () => {
    const { a, posted } = app();
    (a as unknown as { state: unknown }).state = { name: "active" };
    const ev = (a as unknown as { onEvent: (e: unknown) => Promise<void> }).onEvent.bind(a);
    await ev({ event: "closed", data: '{"close_reason":"timed_out"}' });
    expect(posted).toContainEqual({ v: 1, type: "vektra-assess-closed", reason: "timed_out" });
  });
});

// jsdom has no <dialog> behaviour (same minimal polyfill as controls.test.ts)
function dialogPolyfill() {
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  proto.showModal = function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  proto.close = function (this: HTMLDialogElement, rv?: string) {
    this.removeAttribute("open");
    (this as unknown as { returnValue: string }).returnValue = rv ?? "";
    this.dispatchEvent(new Event("close"));
  };
}

describe("REQ-103 flag", () => {
  it("one confirmation, one POST, the marker replaces the control; none after closed", async () => {
    const calls: string[] = [];
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
      new Bridge(
        { parent: { postMessage: () => {} }, addEventListener: () => {} },
        "https://m.example",
      ),
      root,
      (async (url: string, init: RequestInit) => {
        calls.push(`${init.method ?? "GET"} ${url}`);
        return new Response("{}", { status: 201 });
      }) as unknown as typeof fetch,
    );
    const priv = a as unknown as { state: unknown; sid: string };
    priv.state = { name: "active" };
    priv.sid = "S";
    a.chat.render([
      { turn_no: 1, role: "bot", kind: "opening", text: "Benvenuto" },
      { turn_no: 2, role: "student", kind: "normal", text: "ciao" },
    ]);
    expect(a.chat.list.querySelectorAll("button.flag").length).toBe(1); // bot turns only
    root.append(a.chat.root);
    dialogPolyfill();
    const flag = a.chat.list.querySelector("button.flag") as HTMLButtonElement;
    window.confirm = () => {
      throw new Error("window.confirm is blocked in the plugin's sandbox");
    };
    // Annulla: nothing is sent, the focus goes back to the control
    flag.click();
    expect(a.flagDialog.dialog.hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(a.flagDialog.no);
    a.flagDialog.no.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toEqual([]);
    expect(document.activeElement).toBe(flag);
    // one confirmation, one POST
    flag.click();
    a.flagDialog.yes.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toEqual(["POST /api/sessions/S/turns/1/flag"]);
    expect(a.chat.list.querySelector(".flag-marker")!.textContent).toBe("segnalato dallo studente");
    a.chat.render([{ turn_no: 3, role: "bot", kind: "normal", text: "x" }]);
    const ev = (a as unknown as { onEvent: (e: unknown) => Promise<void> }).onEvent.bind(a);
    await ev({ event: "closed", data: '{"close_reason":"finished"}' });
    expect(root.querySelectorAll("button.flag").length).toBe(0);
  });
});

describe("resume without sid and the download-token timeout", () => {
  function host(fetchImpl: typeof fetch) {
    const posted: unknown[] = [];
    let listener: (ev: MessageEvent) => void = () => {};
    const parent = { postMessage: (m: unknown) => posted.push(m) };
    const win = {
      parent,
      addEventListener: (_t: "message", l: (ev: MessageEvent) => void) => (listener = l),
    };
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
      new Bridge(win, "https://m.example"),
      root,
      fetchImpl,
    );
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
    };
    a.start();
    const send = (data: unknown) =>
      listener({ data, origin: "https://m.example", source: parent } as unknown as MessageEvent);
    return { a, posted, send, root };
  }

  it("a resume without sid finds the open session through GET /state", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push(`${url} ${(init.headers as Record<string, string>).Authorization}`);
      if (url === "/api/state") return Response.json({ state: "active", session_id: "S9" });
      if (url === "/api/sessions/S9")
        return Response.json({
          session_id: "S9",
          state: "ACTIVE",
          close_reason: null,
          deadline_at: new Date(Date.now() + 600000).toISOString(),
          server_now: new Date().toISOString(),
          reply_pending: false,
          turns: [],
          token: "SESSION",
        });
      return new Promise<Response>(() => {}); // the event stream stays open
    }) as unknown as typeof fetch;
    const { a, send } = host(fetchImpl);
    send({ v: 1, type: "vektra-assess-token", token: "PRE", mode: "resume" });
    await new Promise((r) => setTimeout(r, 10));
    expect(calls.slice(0, 2)).toEqual(["/api/state Bearer PRE", "/api/sessions/S9 Bearer PRE"]);
    expect((a as unknown as { state: { name: string } }).state.name).toBe("active");
  });

  it("Invia sends with a click on a plain button (no form submission, no allow-forms)", async () => {
    const calls: string[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      calls.push(`${init.method ?? "GET"} ${url} ${String(init.body)}`);
      return Response.json({ session_id: "S", turn_no: 3 }, { status: 202 });
    }) as unknown as typeof fetch;
    const { a, root } = host(fetchImpl);
    const priv = a as unknown as { state: unknown; sid: string };
    priv.state = { name: "active" };
    priv.sid = "S";
    root.append(a.chat.root);
    a.chat.form.addEventListener("submit", () => {
      throw new Error("no form submission");
    });
    expect(a.chat.send.type).toBe("button");
    expect(a.chat.form.querySelector("[type=submit]")).toBeNull();
    a.chat.setEnabled(true);
    a.chat.composer.value = "ciao";
    a.chat.send.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toEqual(['POST /api/sessions/S/turns {"text":"ciao"}']);
  });

  it("an unanswered download-token request times out to a generic text, once", async () => {
    const { a, posted, root } = host((async () => Response.json({})) as unknown as typeof fetch);
    const priv = a as unknown as { state: unknown; sid: string };
    priv.state = { name: "active" };
    priv.sid = "S";
    const ev = (a as unknown as { onEvent: (e: unknown) => Promise<void> }).onEvent.bind(a);
    vi.useFakeTimers();
    await ev({ event: "closed", data: '{"close_reason":"finished"}' });
    const button = root.querySelector(".closing button") as HTMLButtonElement;
    button.click();
    button.click(); // a second click while waiting asks nothing more
    const requests = () =>
      posted.filter((m) => (m as { type: string }).type === "vektra-assess-download-token-request");
    expect(requests().length).toBe(1);
    expect(button.disabled).toBe(true);
    vi.advanceTimersByTime(DOWNLOAD_TIMEOUT_MS + 1);
    expect(root.querySelector(".download-status")!.textContent).toBe(
      "Non è stato possibile scaricare la trascrizione. Riprova più tardi.",
    );
    expect(button.disabled).toBe(false);
    vi.advanceTimersByTime(5 * DOWNLOAD_TIMEOUT_MS);
    expect(requests().length).toBe(1); // no automatic retry
    vi.useRealTimers();
  });
});

describe("review #5: outage_end and a pending reply", () => {
  it("does not enable the composer while the pending turn is still unanswered", async () => {
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
      new Bridge({ parent: { postMessage: () => {} }, addEventListener: () => {} }, "https://m.example"),
      root,
      (async () => Response.json({})) as unknown as typeof fetch,
    );
    const priv = a as unknown as {
      state: unknown;
      sid: string;
      apply: (v: unknown) => void;
      onEvent: (e: unknown) => Promise<void>;
    };
    priv.state = { name: "resuming" };
    priv.sid = "S";
    const view = (state: string, pending: boolean) => ({
      session_id: "S",
      state,
      close_reason: null,
      deadline_at: new Date(Date.now() + 600000).toISOString(),
      server_now: new Date().toISOString(),
      reply_pending: pending,
      turns: [{ turn_no: 1, role: "student", kind: "normal", text: "ciao" }],
    });
    priv.apply.call(a, view("OUTAGE", true)); // the outage froze an unanswered turn
    expect(a.chat.composer.disabled).toBe(true);
    await priv.onEvent.call(a, { event: "outage_end", data: "{}" });
    expect((a as unknown as { state: { name: string } }).state.name).toBe("active");
    expect(a.chat.composer.disabled).toBe(true); // the server still owes the reply
    expect(root.querySelector("#composer-help")!.textContent).toBe(
      "Attendi la risposta prima di scrivere ancora.",
    );
    // the reply arrives: the persisted turns say nothing is pending, the composer opens
    a.chat.add({ turn_no: 2, role: "bot", kind: "normal", text: "ok" });
    priv.apply.call(a, view("ACTIVE", false));
    expect(a.chat.composer.disabled).toBe(false);
  });
});

describe("review #10: Fine and flag failures", () => {
  function make(fetchImpl: typeof fetch) {
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
      new Bridge({ parent: { postMessage: () => {} }, addEventListener: () => {} }, "https://m.example"),
      root,
      fetchImpl,
    );
    const priv = a as unknown as { state: { name: string }; sid: string; finish: () => Promise<void> };
    priv.state = { name: "active" };
    priv.sid = "S";
    root.append(a.chat.root);
    return { a, root, priv };
  }
  const active = {
    session_id: "S",
    state: "ACTIVE",
    close_reason: null,
    deadline_at: new Date(Date.now() + 600000).toISOString(),
    server_now: new Date().toISOString(),
    reply_pending: false,
    turns: [{ turn_no: 1, role: "bot", kind: "opening", text: "Benvenuto" }],
  };

  it("a refused Fine says so and re-reads the session", async () => {
    const calls: string[] = [];
    const { a, root, priv } = make((async (url: string, init: RequestInit) => {
      calls.push(`${init.method ?? "GET"} ${url}`);
      if (url.endsWith("/finish")) return new Response("", { status: 503 });
      return Response.json(active);
    }) as unknown as typeof fetch);
    await priv.finish.call(a);
    expect(calls).toEqual(["POST /api/sessions/S/finish", "GET /api/sessions/S"]);
    expect(priv.state.name).toBe("active"); // nothing closed on the server: still active
    expect(root.querySelector("#status")!.textContent).toBe(
      "La discussione non è stata conclusa. Riprova.",
    );
    expect(a.controls.finish.disabled).toBe(false);
  });

  it("a Fine the network loses re-reads too, and a session closed meanwhile shows closed", async () => {
    const { a, priv } = make((async (url: string) => {
      if (url.endsWith("/finish")) throw new TypeError("network");
      return Response.json({ ...active, state: "CLOSED", close_reason: "finished" });
    }) as unknown as typeof fetch);
    await priv.finish.call(a);
    expect(priv.state.name).toBe("closed");
  });

  it("a flag that fails shows a message and keeps the control", async () => {
    dialogPolyfill();
    const { a } = make((async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch);
    a.chat.render(active.turns as never);
    const flag = a.chat.list.querySelector("button.flag") as HTMLButtonElement;
    flag.click();
    a.flagDialog.yes.click();
    await new Promise((r) => setTimeout(r, 0));
    const article = a.chat.list.querySelector("article")!;
    expect(article.querySelector(".flag-error")!.textContent).toBe(
      "Segnalazione non inviata. Riprova.",
    );
    expect(article.querySelector("button.flag")).not.toBeNull();
    expect(article.querySelector(".flag-marker")).toBeNull();
  });

  it("a flag the server refuses shows the same message", async () => {
    dialogPolyfill();
    const { a } = make((async () => new Response("", { status: 429 })) as unknown as typeof fetch);
    a.chat.render(active.turns as never);
    (a.chat.list.querySelector("button.flag") as HTMLButtonElement).click();
    a.flagDialog.yes.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(a.chat.list.querySelector(".flag-error")!.textContent).toBe(
      "Segnalazione non inviata. Riprova.",
    );
  });
});

// --- CodeRabbit round 1 on PR #157 ------------------------------------------------------------

type Deferred = { url: string; resolve: (r: Response) => void };

function harness() {
  const pending: Deferred[] = [];
  const fetchImpl = ((url: string) =>
    new Promise<Response>((resolve) => pending.push({ url, resolve }))) as unknown as typeof fetch;
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const a = new App(
    { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
    new Bridge({ parent: { postMessage: () => {} }, addEventListener: () => {} }, "https://m.example"),
    root,
    fetchImpl,
  );
  const priv = a as unknown as {
    state: { name: string };
    sid: string;
    onEvent: (e: unknown) => Promise<void>;
    sendTurn: () => Promise<void>;
  };
  priv.state = { name: "active" };
  priv.sid = "S";
  root.append(a.chat.root);
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { a, priv, pending, flush, root };
}

function view(turns: { turn_no: number; role: string; text: string }[], extra: object = {}) {
  return Response.json({
    session_id: "S",
    state: "ACTIVE",
    close_reason: null,
    deadline_at: new Date(Date.now() + 600000).toISOString(),
    server_now: new Date().toISOString(),
    reply_pending: false,
    remaining_s: 600,
    turns: turns.map((t) => ({ kind: t.turn_no === 1 ? "opening" : "normal", ...t })),
    ...extra,
  });
}

describe("snapshot ordering (4501070026, 4501070021, review 5811279241 app.ts 295-301)", () => {
  it("an older session read that arrives last does not replace the newer view", async () => {
    const { priv, pending, flush, root } = harness();
    (priv as unknown as { replyPending: boolean }).replyPending = true; // a reply is owed
    const one = priv.onEvent({ event: "reply_done", data: "{}" });
    const two = priv.onEvent({ event: "reply_done", data: "{}" });
    await flush();
    expect(pending.map((p) => p.url)).toEqual(["/api/sessions/S", "/api/sessions/S"]);
    pending[1].resolve(view([{ turn_no: 1, role: "bot", text: "a" }, { turn_no: 2, role: "student", text: "b" }]));
    await two;
    pending[0].resolve(view([{ turn_no: 1, role: "bot", text: "a" }]));
    await one;
    expect(root.querySelectorAll("article.turn").length).toBe(2);
  });

  it("a session read that completes after the close is ignored", async () => {
    const { priv, pending, flush, root } = harness();
    (priv as unknown as { replyPending: boolean }).replyPending = true; // a reply is owed
    const late = priv.onEvent({ event: "reply_done", data: "{}" });
    await flush();
    await priv.onEvent({ event: "closed", data: '{"close_reason":"finished"}' });
    pending[0].resolve(view([{ turn_no: 1, role: "bot", text: "a" }]));
    await late;
    expect(priv.state.name).toBe("closed");
    expect(root.querySelectorAll("button.flag").length).toBe(0);
  });

  it("a 202 read after a snapshot that already holds the turn adds nothing and owes nothing", async () => {
    const { a, priv, pending, flush, root } = harness();
    a.chat.composer.value = "ciao";
    const post = priv.sendTurn();
    await flush();
    // the reply completed and its snapshot (with the student turn) is applied first
    const done = priv.onEvent({ event: "reply_done", data: "{}" });
    await flush();
    pending[1].resolve(
      view([
        { turn_no: 1, role: "bot", text: "a" },
        { turn_no: 2, role: "student", text: "ciao" },
        { turn_no: 3, role: "bot", text: "risposta" },
      ]),
    );
    await done;
    pending[0].resolve(Response.json({ session_id: "S", turn_no: 2 }, { status: 202 }));
    await post;
    expect(root.querySelectorAll("article.turn").length).toBe(3);
    expect(a.chat.composer.disabled).toBe(false);
  });
});

describe("the snapshot after the commit (4501067234, 4501070029)", () => {
  function preparing() {
    const h = harness();
    h.priv.state = { name: "preparing" } as never;
    (h.a as unknown as { snapshotRetryMs: number[] }).snapshotRetryMs = [0, 0];
    return h;
  }
  const opening = () => view([{ turn_no: 1, role: "bot", text: "Benvenuto" }]);

  it("a failed read after committed is retried; nothing opens until it loads", async () => {
    const { a, priv, pending, flush } = preparing();
    const ev = priv.onEvent({ event: "committed", data: "{}" });
    await flush();
    pending[0].resolve(new Response("", { status: 503 }));
    await flush();
    expect(priv.state.name).toBe("preparing");
    expect(a.chat.list.querySelectorAll("article").length).toBe(0); // nothing shown yet
    await flush();
    pending[1].resolve(opening());
    await ev;
    expect(priv.state.name).toBe("active");
    expect(a.chat.list.querySelectorAll("article").length).toBe(1);
  });

  it("when every retry fails the discussion is unavailable, never an empty open chat", async () => {
    const { priv, pending, flush } = preparing();
    const ev = priv.onEvent({ event: "committed", data: "{}" });
    for (let i = 0; i < 3; i++) {
      await flush();
      await flush();
      pending[i]?.resolve(new Response("", { status: 503 }));
    }
    await ev;
    expect(priv.state.name).toBe("unavailable");
  });

  it("an ACTIVE state snapshot without a committed event loads the session", async () => {
    const { priv, pending, flush } = preparing();
    const ev = priv.onEvent({ event: "state", data: '{"state":"ACTIVE"}' });
    await flush();
    expect(pending.map((p) => p.url)).toEqual(["/api/sessions/S"]);
    pending[0].resolve(opening());
    await ev;
    expect(priv.state.name).toBe("active");
  });
});

describe("outage and unavailable (4501067236, 4501070051)", () => {
  it("an outage is announced once, with Riprova", async () => {
    const { a, priv } = harness();
    await priv.onEvent({ event: "outage_start", data: "{}" });
    expect(a.live.el.textContent).toBe(
      "Assistente temporaneamente non disponibile: questo tempo non ti viene conteggiato. Puoi usare Riprova.",
    );
  });

  it("unavailable is announced and disables every session action", () => {
    dialogPolyfill();
    const { a, priv } = harness();
    (a as unknown as { render: (b: unknown) => void }).render.call(a, { name: "active" });
    a.controls.finish.click(); // the Fine dialog is open when the stream dies
    (a as unknown as { dispatch: (x: unknown) => void }).dispatch.call(a, { type: "unavailable" });
    expect(priv.state.name).toBe("unavailable");
    expect(a.live.el.textContent).toBe(
      "La discussione non è disponibile in questo momento. Riprova più tardi.",
    );
    expect(a.chat.composer.disabled).toBe(true);
    expect(a.controls.finish.disabled).toBe(true);
    expect(a.controls.dialog.hasAttribute("open")).toBe(false);
  });
});

describe("a failed send after the state changed (4501070043)", () => {
  for (const [label, answer] of [
    ["a lost request", null],
    ["an error status", new Response("", { status: 500 })],
  ] as const) {
    it(`${label} during an outage leaves the composer closed`, async () => {
      const { a, priv, pending, flush } = harness();
      (a as unknown as { render: (b: unknown) => void }).render.call(a, { name: "active" });
      a.chat.composer.value = "ciao";
      const post = priv.sendTurn();
      await flush();
      await priv.onEvent({ event: "outage_start", data: "{}" });
      if (answer) pending[0].resolve(answer);
      else (pending[0] as unknown as { resolve: (r: unknown) => void }).resolve(Promise.reject(new TypeError("net")));
      await post;
      expect(priv.state.name).toBe("outage");
      expect(a.chat.composer.disabled).toBe(true);
    });
  }
});

describe("resuming into an outage (4501070038, 4501070048)", () => {
  it("shows the outage banner and the frozen time left", async () => {
    const { a, priv, pending, flush, root } = harness();
    priv.state = { name: "resuming" };
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    pending[0].resolve(
      view([{ turn_no: 1, role: "bot", text: "a" }], {
        state: "OUTAGE",
        reply_pending: true,
        remaining_s: 125,
      }),
    );
    await read;
    expect(priv.state.name).toBe("outage");
    const banner = root.querySelector(".banner") as HTMLElement;
    expect(banner.hidden).toBe(false);
    expect(banner.textContent).toContain("temporaneamente non disponibile");
    expect(a.controls.timer.textContent).toBe("Mancano 3 minuti");
  });
});

describe("the outage banner timer (4501070034)", () => {
  it("a new outage within 8 s of the recovery keeps its banner", async () => {
    vi.useFakeTimers();
    const { priv, root } = harness();
    priv.state = { name: "outage" };
    await priv.onEvent({ event: "outage_end", data: "{}" });
    vi.advanceTimersByTime(3000);
    await priv.onEvent({ event: "outage_start", data: "{}" });
    vi.advanceTimersByTime(6000); // the recovery's hide timer would fire here
    const banner = root.querySelector(".banner") as HTMLElement;
    expect(banner.hidden).toBe(false);
    expect(banner.textContent).toContain("temporaneamente non disponibile");
    vi.useRealTimers();
  });
});

describe("download timers (4501067245)", () => {
  it("the first request's timer never cuts a second request short", async () => {
    vi.useFakeTimers();
    const { a, priv } = harness();
    const ev = priv.onEvent.bind(a);
    await ev({ event: "closed", data: '{"close_reason":"finished"}' });
    const button = () => document.querySelector(".closing button") as HTMLButtonElement;
    button().click(); // request 1 at t=0
    void (a as unknown as { onMessage: (m: unknown) => Promise<void> }).onMessage.call(a, {
      v: 1,
      type: "vektra-assess-download-token",
      tid: "S",
      token: "T1",
    }); // its transcript fetch stays pending: only the token's arrival matters here
    await Promise.resolve();
    vi.advanceTimersByTime(5000);
    button().click(); // request 2 at t=5 s
    expect(button().disabled).toBe(true);
    vi.advanceTimersByTime(5500); // t=10.5 s: request 1's deadline, not request 2's
    expect(button().disabled).toBe(true);
    vi.advanceTimersByTime(5000); // t=15.5 s: request 2 times out
    expect(button().disabled).toBe(false);
    vi.useRealTimers();
  });
});

describe("the flag dialog at the close (4501067248)", () => {
  it("is closed when the session closes", async () => {
    dialogPolyfill();
    const { a, priv } = harness();
    a.chat.render([{ turn_no: 1, role: "bot", kind: "opening", text: "a" }]);
    (a.chat.list.querySelector("button.flag") as HTMLButtonElement).click();
    expect(a.flagDialog.dialog.hasAttribute("open")).toBe(true);
    await priv.onEvent({ event: "closed", data: '{"close_reason":"timed_out"}' });
    expect(a.flagDialog.dialog.hasAttribute("open")).toBe(false);
  });
});

describe("409 on a session read (4501070018, 4501067255)", () => {
  const conflict = (code: string) =>
    Response.json({ detail: { error: { code, remediation: "r" } } }, { status: 409 });

  it("a conflict that is not the close leaves the session open", async () => {
    const { a, priv, pending, flush } = harness();
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    pending[0].resolve(conflict("ERR-ASSESS-011"));
    await read;
    expect(priv.state.name).toBe("active");
  });

  it("the close (ERR-ASSESS-009) closes, and a later closed event names the reason", async () => {
    const posted: { type: string; reason?: string }[] = [];
    const { a, priv, pending, flush } = harness();
    (a as unknown as { bridge: { send: (m: never) => void } }).bridge.send = (m) => posted.push(m);
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    pending[0].resolve(conflict("ERR-ASSESS-009"));
    await read;
    expect(priv.state).toEqual({ name: "closed", reason: "unknown" });
    await priv.onEvent({ event: "closed", data: '{"close_reason":"budget_exhausted"}' });
    expect(priv.state).toEqual({ name: "closed", reason: "budget_exhausted" });
    // one notice, with the reason (the 409's "unknown" waits for it: 4501067255 follow-up)
    expect(posted.filter((m) => m.type === "vektra-assess-closed").map((m) => m.reason)).toEqual([
      "budget_exhausted",
    ]);
  });
});

describe("an accepted Start whose body cannot be read (4501070014)", () => {
  it("never stays preparing: it tells the student to reload and resume", async () => {
    const { a, priv, pending, flush, root } = harness();
    priv.state = { name: "preparing", announced: false } as never;
    const start = (a as unknown as { startSession: () => Promise<void> }).startSession.call(a);
    await flush();
    pending[0].resolve(new Response("{not json", { status: 202 }));
    await start;
    expect(priv.state.name).toBe("unavailable");
    expect(root.querySelector("#status")!.textContent).toBe(
      "La discussione è stata avviata: ricarica la pagina per riprenderla.",
    );
  });
});

describe("the composer limit (4501070009)", () => {
  it("is the server's limit from the page config", () => {
    document.body.innerHTML = "";
    const cfg = document.createElement("script");
    cfg.id = "cfg";
    cfg.type = "application/json";
    cfg.textContent = JSON.stringify({ site: "s", parentOrigin: "https://m.example", apiBase: "/api", maxMessageChars: 3500 });
    document.body.append(cfg);
    const read = readConfig(document);
    const a = new App(
      read,
      new Bridge({ parent: { postMessage: () => {} }, addEventListener: () => {} }, "https://m.example"),
      document.createElement("main"),
    );
    expect(a.chat.composer.maxLength).toBe(7000); // 2 UTF-16 units per code point at most
  });
});

describe("replayed reply events after a resume (4501070057)", () => {
  it("are ignored when no reply is owed: no duplicate text, no announcement, no read", async () => {
    const { a, priv, pending } = harness();
    // the resume snapshot already holds the reply and owes nothing
    (a as unknown as { replyPending: boolean }).replyPending = false;
    await priv.onEvent({ event: "reply_delta", data: '{"text":"vecchia risposta"}' });
    await priv.onEvent({ event: "reply_done", data: "{}" });
    expect(a.chat.list.querySelector("li.streaming")).toBeNull();
    expect(a.live.announcements).toBe(0);
    expect(pending.length).toBe(0);
  });

  it("are shown while a reply is owed", async () => {
    const { a, priv } = harness();
    (a as unknown as { replyPending: boolean }).replyPending = true;
    await priv.onEvent({ event: "reply_delta", data: '{"text":"nuova"}' });
    expect(a.chat.list.querySelector("li.streaming")!.textContent).toContain("nuova");
  });
});

describe("the composer limit counts code points like the server (4501070009)", () => {
  function limited(max: number) {
    const calls: string[] = [];
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api", maxMessageChars: max },
      new Bridge({ parent: { postMessage: () => {} }, addEventListener: () => {} }, "https://m.example"),
      root,
      (async (url: string, init: RequestInit) => {
        calls.push(String(init.body));
        return Response.json({ session_id: "S", turn_no: 2 }, { status: 202 });
      }) as unknown as typeof fetch,
    );
    const priv = a as unknown as { state: unknown; sid: string; sendTurn: () => Promise<void> };
    priv.state = { name: "active" };
    priv.sid = "S";
    root.append(a.chat.root);
    return { a, priv, calls, root };
  }

  it("a non-BMP character is one: 😀a fits a limit of 2", async () => {
    const { a, priv, calls } = limited(2);
    expect(a.chat.composer.maxLength).toBeGreaterThanOrEqual(4); // never stricter than the server
    a.chat.composer.value = "😀a";
    await priv.sendTurn();
    expect(calls).toEqual(['{"text":"😀a"}']);
  });

  it("one code point over the limit is not sent and says why", async () => {
    const { a, priv, calls, root } = limited(2);
    a.chat.composer.value = "😀ab";
    await priv.sendTurn();
    expect(calls).toEqual([]);
    expect(root.querySelector("#composer-help")!.textContent).toBe(
      "Il messaggio è troppo lungo: al massimo 2 caratteri.",
    );
    expect(a.chat.composer.disabled).toBe(false);
  });
});

describe("replay of an earlier reply while a later turn is pending (4501070057 follow-up)", () => {
  it("drops the old reply's replay and keeps the pending reply", async () => {
    const { a, priv, pending, flush, root } = harness();
    // resume snapshot: opening (1), student (2), bot reply (3) done, student (4) pending
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    pending[0].resolve(
      view(
        [
          { turn_no: 1, role: "bot", text: "Benvenuto" },
          { turn_no: 2, role: "student", text: "prima" },
          { turn_no: 3, role: "bot", text: "vecchia risposta" },
          { turn_no: 4, role: "student", text: "seconda" },
        ],
        { reply_pending: true },
      ),
    );
    await read;
    // the stream starts without a cursor: the ring replays reply 3, then the pending reply 5
    await priv.onEvent({ event: "reply_delta", data: '{"text":"vecchia risposta"}' });
    await priv.onEvent({ event: "reply_done", data: '{"turn_no":3,"marked":false}' });
    expect(root.querySelector("li.streaming")).toBeNull(); // the replayed text is gone
    expect(a.live.announcements).toBe(0); // and never announced
    expect(pending.length).toBe(1); // no read for an old reply
    await priv.onEvent({ event: "reply_delta", data: '{"text":"nuova"}' });
    expect(root.querySelector("li.streaming")!.textContent).toContain("nuova");
    expect(root.querySelectorAll("article.turn-bot").length).toBe(3); // 1, 3 and the stream
    const done = priv.onEvent({ event: "reply_done", data: '{"turn_no":5,"marked":false}' });
    await flush();
    pending[1].resolve(
      view([
        { turn_no: 1, role: "bot", text: "Benvenuto" },
        { turn_no: 2, role: "student", text: "prima" },
        { turn_no: 3, role: "bot", text: "vecchia risposta" },
        { turn_no: 4, role: "student", text: "seconda" },
        { turn_no: 5, role: "bot", text: "nuova" },
      ]),
    );
    await done;
    expect(a.live.announcements).toBe(1);
    expect(root.querySelectorAll("article.turn").length).toBe(5);
    expect(root.textContent!.match(/vecchia risposta/g)!.length).toBe(1);
  });
});

describe("the close learnt from a 409, on a real stream (4501067255 follow-up)", () => {
  function streamed() {
    let push: (s: string) => void = () => {};
    const events = new ReadableStream<Uint8Array>({
      start(c) {
        push = (s) => c.enqueue(new TextEncoder().encode(s));
      },
    });
    const fetchImpl = (async (url: string) => {
      if (url.endsWith("/events")) return new Response(events, { status: 200 });
      return Response.json({ detail: { error: { code: "ERR-ASSESS-009", remediation: "r" } } }, { status: 409 });
    }) as unknown as typeof fetch;
    const posted: { type: string; reason?: string }[] = [];
    const root = document.createElement("main");
    document.body.replaceChildren(root);
    const a = new App(
      { site: "s", parentOrigin: "https://m.example", apiBase: "/api" },
      new Bridge(
        { parent: { postMessage: (m: unknown) => posted.push(m as never) }, addEventListener: () => {} },
        "https://m.example",
      ),
      root,
      fetchImpl,
    );
    const priv = a as unknown as {
      state: { name: string; reason?: string };
      sid: string;
      token: string;
      closeReasonWaitMs: number;
      openStream: () => void;
      refresh: () => Promise<unknown>;
    };
    priv.state = { name: "active" };
    priv.sid = "S";
    priv.token = "T";
    priv.closeReasonWaitMs = 200;
    priv.openStream.call(a);
    const closedSent = () => posted.filter((m) => m.type === "vektra-assess-closed").map((m) => m.reason);
    const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
    return { a, priv, push, closedSent, wait, root };
  }

  it("the 409 first, then the closed event: one notice, with the event's reason", async () => {
    const { a, priv, push, closedSent, wait, root } = streamed();
    await priv.refresh.call(a); // the read answers 409 ERR-ASSESS-009 before the event is read
    expect(priv.state).toEqual({ name: "closed", reason: "unknown" });
    expect(closedSent()).toEqual([]); // the parent waits for the reason
    push('event: closed\ndata: {"close_reason":"budget_exhausted"}\n\n');
    await wait(20);
    expect(priv.state).toEqual({ name: "closed", reason: "budget_exhausted" });
    expect(closedSent()).toEqual(["budget_exhausted"]);
    expect(root.querySelector("#closing-title")!.textContent).not.toContain("Sessione conclusa il");
    await wait(250);
    expect(closedSent()).toEqual(["budget_exhausted"]); // once
  });

  it("no closed event within the wait: one notice, unknown", async () => {
    const { a, priv, closedSent, wait } = streamed();
    await priv.refresh.call(a);
    await wait(250);
    expect(closedSent()).toEqual(["unknown"]);
  });
});

describe("a turn sent from another tab (review 5812478913, app.ts 302-320)", () => {
  it("this tab shows the turn, closes its composer and shows the reply", async () => {
    const history = [
      { turn_no: 1, role: "bot", text: "Benvenuto" },
      { turn_no: 2, role: "student", text: "prima" },
      { turn_no: 3, role: "bot", text: "risposta 1" },
    ];
    const tabA = harness();
    const tabB = harness(); // the last one created owns the document; B is the one checked
    for (const t of [tabA, tabB]) (t.a as unknown as { lastTurnNo: number }).lastTurnNo = 3;
    // tab A sends turn 4; the server publishes turn_accepted to every connected tab
    tabA.a.chat.composer.value = "dall'altra tab";
    const sent = tabA.priv.sendTurn();
    await tabA.flush();
    tabA.pending[0].resolve(Response.json({ session_id: "S", turn_no: 4 }, { status: 202 }));
    await sent;
    // tab B: nothing owed until the event says otherwise
    expect((tabB.a as unknown as { replyPending: boolean }).replyPending).toBe(false);
    const accepted = tabB.priv.onEvent({ event: "turn_accepted", data: '{"turn_no":4}' });
    await tabB.flush();
    expect(tabB.a.chat.composer.disabled).toBe(true);
    expect(tabB.pending.map((p) => p.url)).toEqual(["/api/sessions/S"]); // the text: a read
    tabB.pending[0].resolve(
      view([...history, { turn_no: 4, role: "student", text: "dall'altra tab" }], { reply_pending: true }),
    );
    await accepted;
    expect(tabB.root.textContent).toContain("dall'altra tab");
    await tabB.priv.onEvent({ event: "reply_delta", data: '{"text":"risposta 2"}' });
    expect(tabB.root.querySelector("li.streaming")!.textContent).toContain("risposta 2");
    const done = tabB.priv.onEvent({ event: "reply_done", data: '{"turn_no":5,"marked":false}' });
    await tabB.flush();
    tabB.pending[1].resolve(
      view([
        ...history,
        { turn_no: 4, role: "student", text: "dall'altra tab" },
        { turn_no: 5, role: "bot", text: "risposta 2" },
      ]),
    );
    await done;
    expect(tabB.a.live.announcements).toBe(1);
    expect(tabB.a.chat.composer.disabled).toBe(false);
    // a replayed turn_accepted for a turn the snapshot holds changes nothing
    await tabB.priv.onEvent({ event: "turn_accepted", data: '{"turn_no":4}' });
    expect(tabB.pending.length).toBe(2);
  });
});

describe("a snapshot read while a reply is streaming", () => {
  it("keeps the text streamed so far when the reply is still owed", async () => {
    const { a, priv, pending, flush, root } = harness();
    (a as unknown as { replyPending: boolean }).replyPending = true;
    await priv.onEvent({ event: "reply_delta", data: '{"text":"prima parte"}' });
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    pending[0].resolve(
      view(
        [
          { turn_no: 1, role: "bot", text: "Benvenuto" },
          { turn_no: 2, role: "student", text: "domanda" },
        ],
        { reply_pending: true },
      ),
    );
    await read;
    expect(root.querySelector("li.streaming")!.textContent).toContain("prima parte");
    await priv.onEvent({ event: "reply_delta", data: '{"text":", seconda"}' });
    expect(root.querySelector("li.streaming")!.textContent).toContain("prima parte, seconda");
    expect(root.querySelectorAll("li.streaming").length).toBe(1);
  });
});

describe("a session read that predates a state event", () => {
  it("does not undo an outage that started while it was in flight", async () => {
    const { a, priv, pending, flush } = harness();
    (a as unknown as { render: (b: unknown) => void }).render.call(a, { name: "active" });
    const read = (a as unknown as { refresh: () => Promise<unknown> }).refresh.call(a);
    await flush();
    await priv.onEvent({ event: "outage_start", data: "{}" });
    pending[0].resolve(view([{ turn_no: 1, role: "bot", text: "a" }, { turn_no: 2, role: "student", text: "b" }], { reply_pending: true }));
    await read;
    expect(priv.state.name).toBe("outage");
    expect(a.controls.retry.hidden).toBe(false);
    expect(a.chat.list.querySelectorAll("article").length).toBe(2); // its turns still apply
  });
});

describe("transcript download (plan 10)", () => {
  it("fetches with the parent's token as a bearer header and saves under the server's name", async () => {
    const { a, priv, pending, flush } = harness();
    const posted: { type: string; tid?: string }[] = [];
    (a as unknown as { bridge: { send: (m: never) => void } }).bridge.send = (m) => posted.push(m);
    await priv.onEvent({ event: "closed", data: '{"close_reason":"finished"}' });
    (document.querySelector(".closing button") as HTMLButtonElement).click();
    expect(posted.filter((m) => m.type === "vektra-assess-download-token-request")).toEqual([
      { v: 1, type: "vektra-assess-download-token-request", tid: "S" },
    ]);
    const saved: { name: string; size: number }[] = [];
    const created = URL.createObjectURL;
    URL.createObjectURL = () => "blob:x";
    const click = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
      saved.push({ name: this.download, size: 0 });
    };
    try {
      const done = (a as unknown as { onMessage: (m: unknown) => Promise<void> }).onMessage.call(a, {
        v: 1,
        type: "vektra-assess-download-token",
        tid: "S",
        token: "DL-TOKEN",
      });
      await flush();
      const req = pending[pending.length - 1] as unknown as { url: string; resolve: (r: Response) => void };
      expect(req.url).toBe("/api/transcripts/S");
      req.resolve(
        new Response("<!DOCTYPE html><html></html>", {
          status: 200,
          headers: {
            "content-disposition":
              "attachment; filename=\"EA-2026_501_101_s1_20261107.html\"; filename*=UTF-8''EA-2026_501_101_s1_20261107.html",
          },
        }),
      );
      await done;
      await flush();
      expect(saved.map((x) => x.name)).toEqual(["EA-2026_501_101_s1_20261107.html"]);
    } finally {
      URL.createObjectURL = created;
      HTMLAnchorElement.prototype.click = click;
    }
  });

  it("a refused download (404 not generated yet, 401 used token) shows the generic text", async () => {
    const { a, priv, pending, flush, root } = harness();
    await priv.onEvent({ event: "closed", data: '{"close_reason":"finished"}' });
    (document.querySelector(".closing button") as HTMLButtonElement).click();
    const done = (a as unknown as { onMessage: (m: unknown) => Promise<void> }).onMessage.call(a, {
      v: 1,
      type: "vektra-assess-download-token",
      tid: "S",
      token: "T",
    });
    await flush();
    (pending[pending.length - 1] as unknown as { resolve: (r: Response) => void }).resolve(
      new Response("", { status: 404 }),
    );
    await done;
    expect(root.querySelector(".download-status")!.textContent).toMatch(/Non è stato possibile/);
  });

  it("only a well-formed ASCII .html name from the header is used", () => {
    expect(transcriptFileName('attachment; filename="A_1_2_s1_20261107.html"')).toBe("A_1_2_s1_20261107.html");
    for (const bad of [null, "", 'attachment; filename="../x.html"', 'attachment; filename="x.exe"']) {
      expect(transcriptFileName(bad)).toBe("trascrizione.html");
    }
  });
});
