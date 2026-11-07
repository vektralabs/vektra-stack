// The closing screen (REQ-072): one text per close reason, the REQ-093 hint, and the
// transcript download, driven by a download token the plugin mints (postMessage) and fetched
// with the bearer header, saved through an <a download> (the sandbox allows downloads).
import { M } from "../messages.it";
import type { CloseReason } from "../state";
import { h } from "./dom";

export function closingText(reason: CloseReason | "unknown", closedAt: Date | null): string {
  if (reason === "finished") {
    const date = (closedAt ?? new Date()).toLocaleDateString("it-IT", {
      day: "numeric",
      month: "long",
      year: "numeric",
    });
    return M.closing.finished(date);
  }
  return M.closing[reason];
}

export class ClosingView {
  readonly root: HTMLElement;
  readonly download: HTMLButtonElement;
  readonly status: HTMLElement;

  constructor(reason: CloseReason | "unknown", closedAt: Date | null, onDownload: () => void) {
    this.download = h("button", { type: "button", class: "primary", text: M.download });
    this.download.addEventListener("click", onDownload);
    this.status = h("p", { role: "status", class: "download-status" });
    this.root = h(
      "section",
      { class: "closing", "aria-labelledby": "closing-title" },
      h("h2", { id: "closing-title", tabindex: "-1", text: closingText(reason, closedAt) }),
      h("p", { text: M.downloadHint }),
      this.download,
      this.status,
    );
  }

  focus(): void {
    (this.root.querySelector("#closing-title") as HTMLElement).focus();
  }
}

/** Save fetched bytes as a file without leaving the page. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: name, hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
