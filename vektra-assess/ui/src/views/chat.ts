// The conversation (plan 9): the opening message (REQ-094: the system facts block as a semantic
// list, then the orientation), the turn list (<ol> of <article>), the composer, and the
// streaming reply, written into a NON-live element and announced once when it completes.
import { M } from "../messages.it";
import { h, LiveRegion } from "./dom";

export interface Turn {
  turn_no: number;
  role: "student" | "bot" | "system";
  kind: string;
  text: string;
  /** REQ-103 / FEAT-034: a "down" (contested); kept for the turns served before the thumbs */
  flagged?: boolean;
  /** FEAT-034: the student's rating of an assistant reply */
  rating?: "up" | "down" | null;
  /** FEAT-034: a student turn that is a thumbs-down reason names the reply it contests */
  contests_turn?: number | null;
}

/** How much of a contested reply is quoted above the reason (the server's own cap is 400). */
export const QUOTE_CHARS = 200;

export function quoteOf(text: string): string {
  const flat = text.split(/\s+/).filter(Boolean).join(" ");
  return flat.length <= QUOTE_CHARS ? flat : `${flat.slice(0, QUOTE_CHARS - 1).trimEnd()}…`;
}

// a citation tag as the model writes it, "[Domanda 3]", "[Consegna p.2]": the brackets are for
// the post-check, the student reads the label alone (end-to-end test 2026-11-15). Only the label
// shapes the server resolves: a placeholder "[parte filtrata dal controllo automatico: ...]" or an interval "[0, 1]"
// keeps its brackets
const TAG =
  /\[((?:Domanda|Question)\s+[\w.]{1,10}|(?:Criterio|Criterion)\s+\d{1,3}|Valutazione complessiva|Overall assessment|(?:Consegna|Submission)(?:\s+p\.\s?\d{1,4})?)\]/g;

/** Text with line breaks rendered by building elements; "- " lines become list items. A bot
 * reply (``tags``) is shown without the brackets of its citation tags. */
export function richText(text: string, tags = false): HTMLElement {
  const box = h("div", { class: "turn-text" });
  let list: HTMLUListElement | null = null;
  if (tags) text = text.replace(TAG, "$1");
  for (const line of text.split("\n")) {
    if (line.startsWith("- ")) {
      if (!list) {
        list = h("ul");
        box.append(list);
      }
      list.append(h("li", { text: line.slice(2) }));
      continue;
    }
    list = null;
    if (line.trim()) box.append(h("p", { text: line }));
  }
  return box;
}

export interface TurnHooks {
  copy?: (turn: Turn, button: HTMLButtonElement) => void;
  /** FEAT-034 thumbs up (asks once, like the old flag) and thumbs down (opens the reason box) */
  rateUp?: (turn: Turn, article: HTMLElement) => void;
  contest?: (turn: Turn, article: HTMLElement) => void;
}

/** The rating marker of a rated reply, in place of its two buttons. */
export function ratingMarker(rating: "up" | "down", focusable = false): HTMLElement {
  return h("span", {
    class: "flag-marker",
    tabindex: focusable ? "-1" : undefined,
    text: rating === "up" ? M.ratedUp : M.ratedDown,
  });
}

function thumb(cls: string, icon: string, text: string, label: string): HTMLButtonElement {
  // the icon is decoration; the accessible name contains the visible word (WCAG 2.5.3)
  return h(
    "button",
    { type: "button", class: cls, "aria-label": label },
    h("span", { "aria-hidden": "true", text: icon }),
    ` ${text}`,
  );
}

export function turnElement(
  turn: Turn,
  hooks: TurnHooks = {},
  lang = "it",
  quoted: string | null = null,
): HTMLLIElement {
  const label = turn.role === "student" ? M.you : M.assistant;
  // FEAT-034: a thumbs-down reason shows the reply it contests, like a reply in a messenger
  const ref =
    turn.role === "student" && turn.contests_turn
      ? h(
          "div",
          { class: "contest-ref" },
          h("p", { class: "contest-ref-title", text: M.contestRef }),
          quoted ? h("blockquote", { text: quoted }) : null,
        )
      : null;
  const article = h(
    "article",
    { class: `turn turn-${turn.role}`, "aria-label": label, lang, "data-turn": String(turn.turn_no) },
    h("h3", { class: "turn-role", text: label }),
    ref,
    richText(turn.text, turn.role === "bot"),
  );
  const actions = h("div", { class: "turn-actions" });
  if (hooks.copy) {
    const copy = h("button", { type: "button", class: "copy", text: M.copy });
    copy.addEventListener("click", () => hooks.copy!(turn, copy));
    actions.append(copy);
  }
  const rating = turn.rating ?? (turn.flagged ? "down" : null);
  if (rating) actions.append(ratingMarker(rating));
  else if (turn.role === "bot" && hooks.rateUp && hooks.contest) {
    const up = thumb("rate-up", "👍", M.rateUp, M.rateUpLabel);
    up.addEventListener("click", () => hooks.rateUp!(turn, article));
    const down = thumb("rate-down", "👎", M.rateDown, M.rateDownLabel);
    down.addEventListener("click", () => hooks.contest!(turn, article));
    actions.append(up, down);
  }
  if (actions.childNodes.length) article.append(actions);
  return h("li", {}, article);
}

export class ChatView {
  readonly root: HTMLElement;
  readonly list: HTMLOListElement;
  readonly composer: HTMLTextAreaElement;
  readonly send: HTMLButtonElement;
  readonly live: LiveRegion;
  /** FEAT-034: the reason box over the composer, quoting the contested reply */
  readonly contestBox: HTMLElement;
  readonly contestQuote: HTMLQuoteElement;
  readonly contestCancel: HTMLButtonElement;
  /** the reply being contested (its number and its opener), null for an ordinary message */
  contesting: { turn: Turn; opener: HTMLElement | null } | null = null;
  private texts = new Map<number, string>();
  private streaming: { li: HTMLLIElement; text: string } | null = null;

  constructor(
    private readonly hooks: TurnHooks,
    live: LiveRegion,
    maxChars: number,
  ) {
    this.live = live;
    this.list = h("ol", { class: "turns", "aria-label": M.conversation });
    this.composer = h("textarea", {
      id: "composer",
      rows: "3",
      // the limit is enforced in code points on send, like the server (len() of the text);
      // maxlength counts UTF-16 units, so it is only a wide upper bound: 2 units per code point
      maxlength: String(2 * maxChars),
      // the status line (composer-help) and the fixed shortcut hint, both read with the field
      "aria-describedby": "composer-help composer-shortcut",
      "aria-required": "false",
      "aria-keyshortcuts": "Control+Enter Meta+Enter",
    });
    // type="button", never a form submission: the plugin's iframe sandbox has no allow-forms,
    // and the browser blocks a submission before any submit handler runs
    this.send = h("button", { type: "button", class: "primary", text: M.send });
    // Ctrl+Enter (Cmd+Enter on macOS) sends; a plain Enter stays a new line. It is the Invia
    // button's own click, so it is inert exactly when Invia is (composer closed, reply owed)
    this.composer.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || e.isComposing) return;
      e.preventDefault();
      if (!this.composer.disabled && !this.send.disabled) this.send.click();
    });
    this.contestQuote = h("blockquote", { id: "contest-quote" });
    this.contestCancel = h("button", { type: "button", class: "contest-cancel", text: M.contestCancel });
    this.contestCancel.addEventListener("click", () => this.endContest(true));
    this.contestBox = h(
      "div",
      { class: "contest-box", hidden: true },
      h("p", { id: "contest-title", class: "contest-title", text: M.contestTitle }),
      this.contestQuote,
      h("p", { id: "contest-help", class: "contest-help", text: M.contestHelp }),
      this.contestCancel,
    );
    const form = h(
      "form",
      { class: "composer", "aria-label": M.composerLabel },
      this.contestBox,
      h("label", { for: "composer", text: M.composerLabel }),
      this.composer,
      h("p", { id: "composer-help", class: "help", text: "" }),
      h("p", { id: "composer-shortcut", class: "help shortcut", text: M.sendShortcut }),
      this.send,
    );
    this.root = h("section", { class: "chat", "aria-label": M.conversation }, this.list, form);
  }

  get form(): HTMLFormElement {
    return this.root.querySelector("form") as HTMLFormElement;
  }

  /** The persisted turns. ``keepStreaming``: a reply still owed keeps the text streamed so
   *  far after them (a snapshot read mid-stream must not lose it); otherwise the persisted
   *  reply replaces it. */
  render(turns: Turn[], keepStreaming = false): void {
    const streaming = keepStreaming ? this.streaming : null;
    this.texts = new Map(turns.map((t) => [t.turn_no, t.text]));
    this.list.replaceChildren(...turns.map((t) => this.element(t)));
    if (streaming) this.list.append(streaming.li);
    this.streaming = streaming;
  }

  add(turn: Turn): void {
    this.texts.set(turn.turn_no, turn.text);
    this.list.append(this.element(turn));
  }

  private element(turn: Turn): HTMLLIElement {
    const target = turn.contests_turn ? this.texts.get(turn.contests_turn) : undefined;
    return turnElement(turn, this.hooks, "it", target === undefined ? null : quoteOf(target));
  }

  /** FEAT-034 thumbs down: the reason box quotes the reply; the next message is its reason. */
  startContest(turn: Turn, opener: HTMLElement | null): void {
    this.contesting = { turn, opener };
    this.contestQuote.textContent = quoteOf(turn.text);
    this.contestBox.hidden = false;
    this.composer.setAttribute("aria-required", "true");
    this.composer.setAttribute(
      "aria-describedby",
      "contest-title contest-quote contest-help composer-help composer-shortcut",
    );
    if (!this.composer.disabled) this.composer.focus();
  }

  /** Close the reason box; ``cancelled``: the focus goes back to the thumbs-down button. */
  endContest(cancelled = false): void {
    const opener = this.contesting?.opener ?? null;
    this.contesting = null;
    this.contestBox.hidden = true;
    this.contestQuote.textContent = "";
    this.composer.setAttribute("aria-required", "false");
    this.composer.setAttribute("aria-describedby", "composer-help composer-shortcut");
    if (cancelled && opener?.isConnected) opener.focus();
  }

  /** A reply is rated: its buttons give way to the marker, which takes the focus. */
  markRated(turnNo: number, rating: "up" | "down", focus = true): void {
    const article = this.list.querySelector(`article[data-turn="${turnNo}"]`);
    const actions = article?.querySelector(".turn-actions");
    if (!actions) return;
    actions.querySelectorAll("button.rate-up, button.rate-down, .flag-error").forEach((b) => b.remove());
    const marker = ratingMarker(rating, focus);
    actions.append(marker);
    if (focus) marker.focus();
  }

  /** A released segment of the reply: shown, not announced (the element is not live). */
  delta(text: string): void {
    if (!this.streaming) {
      const li = h("li", { class: "streaming", "aria-busy": "true" });
      this.list.append(li);
      this.streaming = { li, text: "" };
    }
    this.streaming.text += text;
    this.streaming.li.replaceChildren(
      h(
        "article",
        { class: "turn turn-bot", "aria-label": M.assistant },
        h("h3", { class: "turn-role", text: M.assistant }),
        richText(this.streaming.text, true),
      ),
    );
  }

  /** Drop the text streamed so far without announcing it (a replayed, already shown reply). */
  discardStreaming(): void {
    this.streaming?.li.remove();
    this.streaming = null;
  }

  /** The reply is complete: one announcement in the polite live region. */
  replyDone(interrupted: boolean): void {
    if (this.streaming) this.streaming.li.removeAttribute("aria-busy");
    this.streaming = null;
    this.live.say(interrupted ? M.replyInterrupted : M.replyDone);
  }

  setEnabled(enabled: boolean, help = ""): void {
    this.composer.disabled = !enabled;
    this.send.disabled = !enabled;
    (this.root.querySelector("#composer-help") as HTMLElement).textContent = help;
  }
}
