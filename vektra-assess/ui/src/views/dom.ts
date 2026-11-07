// The only way the UI builds markup: elements and textContent. No HTML string is ever parsed.
type Attrs = Record<string, string | boolean | undefined>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Node | string | null | undefined)[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (k === "text") el.textContent = String(v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

/** A polite live region; ``say`` announces a text exactly once per call. */
export class LiveRegion {
  readonly el: HTMLElement;
  announcements = 0;
  constructor(label: string) {
    this.el = h("div", { role: "status", "aria-live": "polite", "aria-label": label, class: "sr-only" });
  }
  say(text: string): void {
    this.announcements += 1;
    this.el.textContent = "";
    // a new text node, so assistive technology reports it even when the text repeats
    this.el.append(document.createTextNode(text));
  }
}
