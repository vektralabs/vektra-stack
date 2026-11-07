import { describe, expect, it } from "vitest";
import { Bridge, parseInbound } from "../src/bridge";

const MOODLE = "https://moodle.example";

function fakeWindow() {
  const posted: { message: unknown; target: string }[] = [];
  let listener: ((ev: MessageEvent) => void) | null = null;
  const parent = { postMessage: (message: unknown, target: string) => posted.push({ message, target }) };
  const win = {
    parent,
    addEventListener: (_t: "message", l: (ev: MessageEvent) => void) => (listener = l),
  };
  const dispatch = (data: unknown, origin: string, source: unknown) =>
    listener!({ data, origin, source } as unknown as MessageEvent);
  return { win, posted, dispatch, parent };
}

const TOKEN = { v: 1, type: "vektra-assess-token", token: "T", mode: "start" };

describe("Bridge", () => {
  it("accepts a valid message from the Moodle origin and the parent window", () => {
    const f = fakeWindow();
    const got: unknown[] = [];
    new Bridge(f.win, MOODLE).listen((m) => got.push(m));
    f.dispatch(TOKEN, MOODLE, f.parent);
    expect(got).toEqual([TOKEN]);
  });

  it("ignores a wrong origin, a wrong source, a wrong version or an unknown type", () => {
    const f = fakeWindow();
    const got: unknown[] = [];
    new Bridge(f.win, MOODLE).listen((m) => got.push(m));
    f.dispatch(TOKEN, "https://evil.example", f.parent);
    f.dispatch(TOKEN, "https://moodle.example:8443", f.parent);
    f.dispatch(TOKEN, MOODLE, {}); // another window on the Moodle origin
    f.dispatch({ ...TOKEN, v: 2 }, MOODLE, f.parent);
    f.dispatch({ v: 1, type: "vektra-assess-eval", code: "x" }, MOODLE, f.parent);
    f.dispatch("vektra-assess-token", MOODLE, f.parent);
    expect(got).toEqual([]);
  });

  it("posts with the explicit Moodle origin, never '*'", () => {
    const f = fakeWindow();
    const b = new Bridge(f.win, MOODLE);
    b.send({ v: 1, type: "vektra-assess-ready" });
    b.send({ v: 1, type: "vektra-assess-closed", reason: "finished" });
    expect(f.posted.map((p) => p.target)).toEqual([MOODLE, MOODLE]);
    expect(() => new Bridge(f.win, "*")).toThrow();
    expect(() => new Bridge(f.win, "")).toThrow();
  });

  it("reports height changes once each through resize", () => {
    const f = fakeWindow();
    let cb: () => void = () => {};
    class RO {
      constructor(c: () => void) {
        cb = c;
      }
      observe() {}
    }
    let h = 100;
    const el = { getBoundingClientRect: () => ({ height: h }) } as unknown as Element;
    new Bridge(f.win, MOODLE).watchHeight(el, RO as unknown as typeof ResizeObserver);
    cb();
    cb();
    h = 250.2;
    cb();
    expect(f.posted.map((p) => p.message)).toEqual([
      { v: 1, type: "vektra-assess-resize", height: 100 },
      { v: 1, type: "vektra-assess-resize", height: 251 },
    ]);
  });
});

describe("parseInbound", () => {
  it("validates every field", () => {
    expect(parseInbound({ v: 1, type: "vektra-assess-token", token: "", mode: "start" })).toBeNull();
    expect(parseInbound({ v: 1, type: "vektra-assess-token", token: "T", mode: "resume" })).toEqual({
      v: 1,
      type: "vektra-assess-token",
      token: "T",
      mode: "resume",
    });
    expect(
      parseInbound({ v: 1, type: "vektra-assess-token", token: "T", mode: "resume", sid: 7 }),
    ).toBeNull();
    expect(
      parseInbound({ v: 1, type: "vektra-assess-token", token: "T", mode: "resume", sid: "s" }),
    ).toEqual({ v: 1, type: "vektra-assess-token", token: "T", mode: "resume", sid: "s" });
    expect(parseInbound({ v: 1, type: "vektra-assess-unavailable", extra: 1 })).toEqual({
      v: 1,
      type: "vektra-assess-unavailable",
    });
    expect(parseInbound(null)).toBeNull();
  });
});
