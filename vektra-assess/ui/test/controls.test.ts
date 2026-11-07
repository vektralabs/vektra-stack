// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { ControlsView, minutesLeft } from "../src/views/controls";

// jsdom has no <dialog> behaviour: a minimal one, enough for the focus and return-value logic
function polyfill() {
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

describe("controls", () => {
  it("rounds the minutes left up and never below zero", () => {
    expect(minutesLeft(10 * 60000, 0)).toBe(10);
    expect(minutesLeft(60001, 0)).toBe(2);
    expect(minutesLeft(1, 0)).toBe(1);
    expect(minutesLeft(0, 5)).toBe(0);
  });

  it("the timer is not live and shows minute text", () => {
    const c = new ControlsView(() => {}, () => {});
    expect(c.timer.getAttribute("role")).toBe("timer");
    expect(c.timer.getAttribute("aria-live")).toBe("off");
    c.setTime(1);
    expect(c.timer.textContent).toBe("Manca 1 minuto");
    c.setTime(7);
    expect(c.timer.textContent).toBe("Mancano 7 minuti");
  });

  it("Riprova is shown only in outage", () => {
    let n = 0;
    const c = new ControlsView(() => n++, () => {});
    expect(c.retry.hidden).toBe(true);
    c.setOutage(true);
    expect(c.retry.hidden).toBe(false);
    c.retry.click();
    expect(n).toBe(1);
    c.setOutage(false);
    expect(c.retry.hidden).toBe(true);
  });

  it("Fine asks first; Annulla changes nothing and returns focus to Fine", () => {
    polyfill();
    let finished = 0;
    const c = new ControlsView(() => {}, () => finished++);
    document.body.append(c.root);
    c.finish.click();
    expect(c.dialog.hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(c.confirmNo);
    c.confirmNo.click();
    expect(finished).toBe(0);
    expect(document.activeElement).toBe(c.finish);
    c.finish.click();
    c.confirmYes.click();
    expect(finished).toBe(1);
  });
});
