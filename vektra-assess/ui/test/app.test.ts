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
    let asked = 0;
    window.confirm = () => (asked++, true);
    (a.chat.list.querySelector("button.flag") as HTMLButtonElement).click();
    await new Promise((r) => setTimeout(r, 0));
    expect(asked).toBe(1);
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
