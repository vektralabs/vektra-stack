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
  flagged?: boolean;
}

/** Text with line breaks rendered by building elements; "- " lines become list items. */
export function richText(text: string): HTMLElement {
  const box = h("div", { class: "turn-text" });
  let list: HTMLUListElement | null = null;
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
  flag?: (turn: Turn, article: HTMLElement) => void;
}

export function turnElement(turn: Turn, hooks: TurnHooks = {}, lang = "it"): HTMLLIElement {
  const label = turn.role === "student" ? M.you : M.assistant;
  const article = h(
    "article",
    { class: `turn turn-${turn.role}`, "aria-label": label, lang, "data-turn": String(turn.turn_no) },
    h("h3", { class: "turn-role", text: label }),
    richText(turn.text),
  );
  const actions = h("div", { class: "turn-actions" });
  if (hooks.copy) {
    const copy = h("button", { type: "button", class: "copy", text: M.copy });
    copy.addEventListener("click", () => hooks.copy!(turn, copy));
    actions.append(copy);
  }
  if (turn.flagged) actions.append(h("span", { class: "flag-marker", text: M.flagged }));
  else if (hooks.flag && turn.role === "bot") {
    const flag = h("button", { type: "button", class: "flag", text: M.flag });
    flag.addEventListener("click", () => hooks.flag!(turn, article));
    actions.append(flag);
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
      "aria-describedby": "composer-help",
    });
    // type="button", never a form submission: the plugin's iframe sandbox has no allow-forms,
    // and the browser blocks a submission before any submit handler runs
    this.send = h("button", { type: "button", class: "primary", text: M.send });
    const form = h(
      "form",
      { class: "composer", "aria-label": M.composerLabel },
      h("label", { for: "composer", text: M.composerLabel }),
      this.composer,
      h("p", { id: "composer-help", class: "help", text: "" }),
      this.send,
    );
    this.root = h("section", { class: "chat", "aria-label": M.conversation }, this.list, form);
  }

  get form(): HTMLFormElement {
    return this.root.querySelector("form") as HTMLFormElement;
  }

  render(turns: Turn[]): void {
    this.list.replaceChildren(...turns.map((t) => turnElement(t, this.hooks)));
    this.streaming = null;
  }

  add(turn: Turn): void {
    this.list.append(turnElement(turn, this.hooks));
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
        richText(this.streaming.text),
      ),
    );
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
