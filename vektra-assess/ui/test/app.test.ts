// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { App } from "../src/app";
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
