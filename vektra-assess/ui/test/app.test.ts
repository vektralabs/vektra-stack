// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { App, DOWNLOAD_TIMEOUT_MS } from "../src/app";
import { Bridge } from "../src/bridge";

function app() {
  const posted: unknown[] = [];
  const win = {
    parent: { postMessage: (m: unknown) => posted.push(m) },
    addEventListener: () => {},
  };
  const root = document.createElement("main");
  document.body.replaceChildren(root);
  const a = new App(
    { site: "s", moodleOrigin: "https://m.example", apiBase: "/api/v1/assess" },
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
      { site: "s", moodleOrigin: "https://m.example", apiBase: "/api" },
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
      { site: "s", moodleOrigin: "https://m.example", apiBase: "/api" },
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
      { site: "s", moodleOrigin: "https://m.example", apiBase: "/api" },
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
      { site: "s", moodleOrigin: "https://m.example", apiBase: "/api" },
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
    { site: "s", moodleOrigin: "https://m.example", apiBase: "/api" },
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
