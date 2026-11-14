// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { LiveRegion } from "../src/views/dom";
import { ChatView, richText, turnElement } from "../src/views/chat";

describe("chat view", () => {
  it("renders server text as text, never as markup", () => {
    const evil = '<img src=x onerror="alert(1)">\n&lt;script&gt;';
    const el = richText(evil);
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector("script")).toBeNull();
    expect(el.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("renders the facts block as a semantic list", () => {
    const el = richText("Compito\n- Criterio 1: 10/10\n- Criterio 2: 5/10\nCome procedere");
    expect([...el.querySelectorAll("li")].map((li) => li.textContent)).toEqual([
      "Criterio 1: 10/10",
      "Criterio 2: 5/10",
    ]);
    expect(el.querySelectorAll("p").length).toBe(2);
  });

  it("puts each turn in an article with its role label", () => {
    const li = turnElement({ turn_no: 3, role: "student", kind: "normal", text: "ciao" });
    const art = li.querySelector("article")!;
    expect(art.getAttribute("aria-label")).toBe("Tu");
    expect(art.querySelector("h3")!.textContent).toBe("Tu");
  });

  it("streams into a non-live element and announces once at the end", () => {
    const live = new LiveRegion("Notifiche");
    const view = new ChatView({}, live, 2000);
    document.body.append(view.root, live.el);
    view.delta("Uno. ");
    view.delta("Due.");
    expect(view.list.textContent).toContain("Uno. Due.");
    expect(view.list.querySelector("[aria-live]")).toBeNull();
    expect(live.announcements).toBe(0);
    view.replyDone(false);
    expect(live.announcements).toBe(1);
    expect(live.el.textContent).toBe("Risposta dell'assistente ricevuta");
  });
});

describe("Ctrl/Cmd+Enter sends (live E2E follow-up)", () => {
  function composer() {
    const live = new LiveRegion("Notifiche");
    const view = new ChatView({}, live, 2000);
    document.body.replaceChildren(view.root, live.el);
    let sent = 0;
    view.send.addEventListener("click", () => sent++);
    const key = (init: KeyboardEventInit) => {
      const e = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...init });
      view.composer.dispatchEvent(e);
      return e;
    };
    return { view, key, sent: () => sent };
  }

  it("Ctrl+Enter and Meta+Enter press Invia; a plain Enter stays a new line", () => {
    const { key, sent } = composer();
    const plain = key({});
    expect(sent()).toBe(0);
    expect(plain.defaultPrevented).toBe(false); // the textarea inserts the newline
    expect(key({ shiftKey: true }).defaultPrevented).toBe(false);
    expect(key({ ctrlKey: true }).defaultPrevented).toBe(true);
    expect(sent()).toBe(1);
    key({ metaKey: true });
    expect(sent()).toBe(2);
    key({ ctrlKey: true, key: "a" });
    expect(sent()).toBe(2);
  });

  it("is inert while sending is blocked (composer or Invia disabled)", () => {
    const { view, key, sent } = composer();
    view.setEnabled(false, "Attendi la risposta prima di scrivere ancora.");
    key({ ctrlKey: true });
    view.composer.disabled = false; // only Invia blocked: still inert
    key({ metaKey: true });
    expect(sent()).toBe(0);
    view.setEnabled(true);
    key({ ctrlKey: true });
    expect(sent()).toBe(1);
  });

  it("an IME composition's Enter is never a send", () => {
    const { key, sent } = composer();
    key({ ctrlKey: true, isComposing: true });
    expect(sent()).toBe(0);
  });

  it("states the shortcut under the field without taking over the status line", () => {
    const { view } = composer();
    expect(view.composer.getAttribute("aria-keyshortcuts")).toBe("Control+Enter Meta+Enter");
    const ids = view.composer.getAttribute("aria-describedby")!.split(" ");
    expect(ids).toEqual(["composer-help", "composer-shortcut"]);
    const hint = view.root.querySelector("#composer-shortcut")!;
    expect(hint.textContent).toBe("Ctrl+Invio per inviare");
    view.setEnabled(false, "Il messaggio non è stato inviato. Riprova.");
    expect(view.root.querySelector("#composer-help")!.textContent).toBe(
      "Il messaggio non è stato inviato. Riprova.",
    );
    expect(hint.textContent).toBe("Ctrl+Invio per inviare"); // the status never replaces it
  });
});
