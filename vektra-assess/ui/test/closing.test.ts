// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { ClosingView, closingText } from "../src/views/closing";

describe("closing", () => {
  it("has a text for every close reason", () => {
    const d = new Date(2026, 10, 7, 12); // local time, like the formatter (CodeRabbit 4501067257)
    expect(closingText("finished", d)).toBe(
      "Sessione conclusa il 7 novembre 2026; trascrizione inviata al docente.",
    );
    expect(closingText("terminated_by_teacher", d)).toBe(
      "Sessione chiusa dal docente; trascrizione disponibile.",
    );
    for (const r of ["timed_out", "service_failure", "budget_exhausted", "access_revoked", "unknown"] as const) {
      expect(closingText(r, d).length).toBeGreaterThan(10);
    }
  });

  it("shows the hint and the download button", () => {
    let n = 0;
    const v = new ClosingView("finished", null, () => n++);
    expect(v.root.textContent).toContain("Ti consigliamo di scaricare una copia della trascrizione");
    v.download.click();
    expect(n).toBe(1);
  });
});
