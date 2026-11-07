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
