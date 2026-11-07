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
