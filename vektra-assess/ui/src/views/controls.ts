// In-session controls (REQ-102 minimal set): Invia lives in the composer; here are Copia per
// message, Riprova (visible in OUTAGE only: it asks the server to try now and never ends the
// outage client-side), Fine with a confirmation dialog, and the remaining time.
import { M } from "../messages.it";
import { h } from "./dom";

/** Copy a message's text; falls back to selecting it when the clipboard API is refused. */
export async function copyText(text: string, target: HTMLElement): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const range = document.createRange();
    range.selectNodeContents(target);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    return false;
  }
}

/** Minutes left, rounded up so "1 minute" shows until the very end; text for every user. */
export function minutesLeft(deadlineMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 60000));
}

export class ControlsView {
  readonly root: HTMLElement;
  readonly timer: HTMLElement;
  readonly retry: HTMLButtonElement;
  readonly finish: HTMLButtonElement;
  readonly dialog: HTMLDialogElement;
  readonly confirmYes: HTMLButtonElement;
  readonly confirmNo: HTMLButtonElement;

  constructor(onRetry: () => void, onFinish: () => void) {
    // role=timer is implicitly aria-live=off: it is read on demand, never announced each minute
    this.timer = h("p", { role: "timer", "aria-live": "off", class: "timer" });
    this.retry = h("button", { type: "button", class: "retry", text: M.retry, hidden: true });
    this.retry.addEventListener("click", onRetry);
    this.finish = h("button", { type: "button", class: "finish", text: M.finish });
    this.confirmYes = h("button", { type: "button", class: "primary", text: M.confirmYes });
    this.confirmNo = h("button", { type: "button", text: M.confirmNo });
    this.dialog = h(
      "dialog",
      { "aria-labelledby": "confirm-title", "aria-describedby": "confirm-text" },
      h("h2", { id: "confirm-title", text: M.confirmTitle }),
      h("p", { id: "confirm-text", text: M.confirmText }),
      h("div", { class: "dialog-actions" }, this.confirmNo, this.confirmYes),
    );
    // showModal traps focus inside the dialog; Annulla (and Esc) close it and return focus to
    // Fine, and change nothing on the server
    this.finish.addEventListener("click", () => {
      this.dialog.showModal();
      this.confirmNo.focus();
    });
    this.confirmNo.addEventListener("click", () => this.dialog.close("cancel"));
    this.dialog.addEventListener("close", () => {
      if (this.dialog.returnValue !== "confirm") this.finish.focus();
    });
    this.confirmYes.addEventListener("click", () => {
      this.dialog.close("confirm");
      onFinish();
    });
    this.root = h(
      "div",
      { class: "controls" },
      this.timer,
      h("div", { class: "control-buttons" }, this.retry, this.finish),
      this.dialog,
    );
  }

  setTime(minutes: number | null): void {
    this.timer.textContent =
      minutes === null ? "" : minutes > 0 ? M.timeLeft(minutes) : M.timeLeftNone;
  }

  setOutage(outage: boolean): void {
    this.retry.hidden = !outage;
  }

  disable(): void {
    this.retry.hidden = true;
    this.finish.disabled = true;
  }
}
