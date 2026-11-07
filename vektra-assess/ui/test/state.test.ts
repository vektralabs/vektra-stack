import { describe, expect, it } from "vitest";
import { INITIAL, isTerminal, reduce, type Action, type UiState } from "../src/state";

const run = (actions: Action[], from: UiState = INITIAL) => actions.reduce(reduce, from);

describe("reduce", () => {
  it("start: awaiting -> preparing (announced once, at 202) -> active", () => {
    const prep = run([{ type: "handoff", mode: "start" }]);
    expect(prep).toEqual({ name: "preparing", announced: false });
    expect(reduce(prep, { type: "start_accepted" })).toEqual({ name: "preparing", announced: true });
    expect(run([{ type: "start_accepted" }, { type: "committed" }], prep)).toEqual({ name: "active" });
  });

  it("resume: awaiting -> resuming -> the session's state", () => {
    const res = run([{ type: "handoff", mode: "resume" }]);
    expect(res).toEqual({ name: "resuming" });
    expect(reduce(res, { type: "session_state", state: "ACTIVE" })).toEqual({ name: "active" });
    expect(reduce(res, { type: "session_state", state: "OUTAGE" })).toEqual({ name: "outage" });
    expect(reduce(res, { type: "session_state", state: "STARTING" })).toEqual({
      name: "preparing",
      announced: true,
    });
    expect(reduce(res, { type: "session_state", state: "CLOSED", closeReason: "timed_out" })).toEqual({
      name: "closed",
      reason: "timed_out",
    });
  });

  it("active <-> outage, both directions, only from the right state", () => {
    const active: UiState = { name: "active" };
    const outage = reduce(active, { type: "outage_start" });
    expect(outage).toEqual({ name: "outage" });
    expect(reduce(outage, { type: "outage_end" })).toEqual({ name: "active" });
    expect(reduce(active, { type: "outage_end" })).toEqual(active);
    expect(reduce(outage, { type: "session_state", state: "ACTIVE" })).toEqual(active);
  });

  it("capacity, rollback and unavailable are terminal non-technical states", () => {
    const prep: UiState = { name: "preparing", announced: true };
    expect(reduce(prep, { type: "capacity_refused" })).toEqual({ name: "capacity_refused" });
    expect(reduce(prep, { type: "rolled_back", reason: "capacity" })).toEqual({
      name: "start_rolled_back",
      reason: "capacity",
    });
    expect(reduce(INITIAL, { type: "unavailable" })).toEqual({ name: "unavailable" });
    for (const s of [
      { name: "capacity_refused" },
      { name: "start_rolled_back", reason: "x" },
      { name: "unavailable" },
    ] as UiState[]) {
      expect(isTerminal(s)).toBe(true);
      expect(reduce(s, { type: "unavailable" })).toEqual(s);
    }
  });

  it("closed keeps its reason and never reopens", () => {
    const c = reduce({ name: "active" }, { type: "closed", reason: "finished" });
    expect(c).toEqual({ name: "closed", reason: "finished" });
    for (const a of [
      { type: "committed" },
      { type: "outage_start" },
      { type: "session_state", state: "ACTIVE" },
      { type: "closed", reason: "timed_out" },
    ] as Action[]) {
      expect(reduce(c, a)).toBe(c);
    }
    expect(reduce({ name: "active" }, { type: "closed", reason: "<img>" })).toEqual({
      name: "closed",
      reason: "unknown",
    });
  });

  it("a second handoff is ignored", () => {
    const res: UiState = { name: "active" };
    expect(reduce(res, { type: "handoff", mode: "start" })).toBe(res);
  });
});

describe("the close reason (CodeRabbit 4501067255)", () => {
  it("an unknown close (a 409) takes the reason a later closed event carries, once", () => {
    let s = reduce({ name: "active" }, { type: "closed", reason: null });
    expect(s).toEqual({ name: "closed", reason: "unknown" });
    s = reduce(s, { type: "closed", reason: "budget_exhausted" });
    expect(s).toEqual({ name: "closed", reason: "budget_exhausted" });
    // a known reason is final
    expect(reduce(s, { type: "closed", reason: "finished" })).toBe(s);
  });
});
