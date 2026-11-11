// The discussion UI end to end, framed by the Moodle stub on another origin (plan 9). Every
// user-visible claim is checked against server state (rows, GET /sessions/{sid}).
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { expect, test, type Request } from "@playwright/test";
import {
  botReplies,
  control,
  fromUi,
  hostUrl,
  opened,
  said,
  send,
  serverSession,
  tabTo,
  TEXT,
  THIRD,
  UI,
  uiFrame,
  watchAnnouncements,
  type Target,
} from "./helpers";

/** The text content richText() renders for a stored text: one element per non-empty line, a
 *  "- " line as a list item without its marker. */
function asRendered(text: string): string {
  return text
    .split("\n")
    .filter((l) => l.startsWith("- ") || l.trim())
    .map((l) => (l.startsWith("- ") ? l.slice(2) : l))
    .join("");
}

/** Requests to the assess API (the page and its assets excluded). */
function apiLog(page: import("@playwright/test").Page): string[] {
  const log: string[] = [];
  page.on("request", (r: Request) => {
    const url = r.url();
    if (url.startsWith(`${UI}/api/v1/assess/`) && !url.startsWith(`${UI}/api/v1/assess/ui`))
      log.push(`${r.method()} ${url.slice(UI.length)}`);
  });
  return log;
}

test("a page on a third origin cannot frame the UI", async ({ page }) => {
  const console: string[] = [];
  page.on("console", (m) => console.push(m.text()));
  await page.goto(hostUrl({ who: "stu1", a: "rubric_vis" }, { hold: "1" }, THIRD));
  // Chromium reports the CSP violation and does not render the frame
  await expect.poll(() => console.some((m) => m.includes("frame-ancestors"))).toBe(true);
  for (const f of page.frames()) expect(await f.locator("main#app").count()).toBe(0);
  expect(await fromUi(page)).toEqual([]); // the UI never booted inside that page
});

test("a valid token posted from the wrong origin is ignored: no request", async ({
  page,
  request,
}) => {
  const t: Target = { who: "stu5", a: "rubric_vis" };
  const api = apiLog(page);
  // the host does not hand off (hold); a frame on the third origin posts a freshly minted,
  // valid pre-session token to the UI ten times
  await page.goto(hostUrl(t, { hold: "1", evil: "1" }));
  const ui = await uiFrame(page);
  await expect
    .poll(async () => {
      const evil = page.frames().find((f) => f.url().startsWith(`${THIRD}/evil`));
      return evil ? evil.title() : "";
    })
    .toBe("posted");
  await page.waitForTimeout(500);
  expect(api).toEqual([]);
  await expect(ui.locator("#status")).toHaveText(TEXT.waiting);
  expect((await serverSession(request, t)).session_id).toBeNull();
});

test("start, two turns, warning, outage and Riprova, flag, Annulla and Fine: keyboard only", async ({
  page,
  request,
}) => {
  const t: Target = { who: "stu1", a: "rubric_vis" };
  await page.goto(hostUrl(t));
  const ui = await uiFrame(page);
  await watchAnnouncements(ui);
  await opened(request, ui, t);

  // two turns: the DOM shows exactly the persisted text, one announcement per reply
  const messages = ["Ho scelto la struttura per capitoli.", "Il criterio C1 chiede chiarezza."];
  for (const [i, message] of messages.entries()) {
    await send(page, ui, message);
    await expect.poll(() => botReplies(request, t)).toBe(i + 1);
    await expect.poll(() => said(ui, TEXT.replyDone)).toBe(i + 1);
    const server = await serverSession(request, t);
    const dom = await ui.locator("article.turn .turn-text").allTextContents();
    expect(dom).toEqual(server.turns.map((x) => asRendered(x.text)));
    const lastBot = server.turns.filter((x) => x.role === "bot").at(-1)!;
    expect(await ui.locator("article.turn-bot .turn-text").last().textContent()).toBe(lastBot.text);
    expect(server.turns.filter((x) => x.role === "student").at(-1)!.text).toBe(message);
  }

  // the 5-minute warning: announced once (REQ-069)
  await control(request, "warn", {}, t);
  await expect.poll(() => said(ui, "5 minuti"), { timeout: 30_000 }).toBe(1);
  await page.waitForTimeout(3000);
  expect(await said(ui, "5 minuti")).toBe(1);

  // outage: banner, Riprova does not end it, the server's probe does (REQ-086)
  await control(request, "outage", { on: true });
  await send(page, ui, "E il criterio C2?");
  await expect(ui.locator(".banner")).toContainText(TEXT.outage);
  await expect.poll(async () => (await serverSession(request, t)).state).toBe("OUTAGE");
  await tabTo(page, ui, "button.retry");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1500);
  expect((await serverSession(request, t)).state).toBe("OUTAGE");
  await expect(ui.locator("button.retry")).toBeVisible();
  await control(request, "outage", { on: false });
  await expect(ui.locator(".banner")).toContainText(TEXT.outageEnd, { timeout: 30_000 });
  await expect.poll(() => botReplies(request, t), { timeout: 30_000 }).toBe(3);
  await expect.poll(() => said(ui, TEXT.replyDone)).toBe(3);
  expect((await serverSession(request, t)).state).toBe("ACTIVE");

  // REQ-103 flag, inside the plugin's sandbox (no allow-modals): an in-page dialog asks once
  await tabTo(page, ui, "button.flag");
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toBeVisible();
  await page.keyboard.press("Tab"); // Annulla -> Segnala
  await page.keyboard.press("Enter");
  await expect(ui.locator(".flag-marker")).toHaveCount(1);
  await expect
    .poll(async () => (await serverSession(request, t)).turns.filter((x) => x.flagged).length)
    .toBe(1);

  // Fine, Annulla: nothing changes on the server, the focus returns to Fine (REQ-072)
  const before = await serverSession(request, t);
  await tabTo(page, ui, "button.finish");
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toBeVisible();
  expect(await ui.evaluate(() => document.activeElement?.textContent)).toBe("Annulla");
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toHaveCount(0);
  expect(await ui.evaluate(() => document.activeElement?.className)).toContain("finish");
  const after = await serverSession(request, t);
  expect(after.state).toBe("ACTIVE");
  expect(after.deadline_at).toBe(before.deadline_at);

  // Fine, Concludi: CLOSED(finished), exactly one transcript job, the closing screen
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toBeVisible();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(ui.locator("#closing-title")).toContainText("Sessione conclusa");
  const closed = await serverSession(request, t);
  expect([closed.state, closed.close_reason, closed.transcript_jobs]).toEqual([
    "CLOSED",
    "finished",
    1,
  ]);
  await expect(ui.locator("#composer")).toBeDisabled();
  expect(await fromUi(page)).toContain("vektra-assess-closed");
});

test("resume after a reload: the plugin sends no sid; an explicit sid works too", async ({
  page,
  request,
}) => {
  const t: Target = { who: "stu1", a: "scale" };
  await page.goto(hostUrl(t));
  let ui = await uiFrame(page);
  await opened(request, ui, t);
  await send(page, ui, "Prima del ricaricamento.");
  await expect.poll(() => botReplies(request, t)).toBe(1);
  let server = await serverSession(request, t);

  // the plugin's resume: mode "resume" and no sid. Leave the old page first: its UI keeps
  // polling the open session, and a GET /sessions/{sid} still in flight from it would land in
  // the log ahead of the new page's /state lookup.
  await page.goto("about:blank");
  let api = apiLog(page);
  await page.goto(hostUrl(t, { mode: "resume" }));
  ui = await uiFrame(page);
  await expect(ui.locator("article.turn")).toHaveCount(server.turns.length);
  // the open session is found through GET /state with the pre-session token
  expect(api.slice(0, 2)).toEqual([
    "GET /api/v1/assess/state",
    `GET /api/v1/assess/sessions/${server.session_id}`,
  ]);
  // the re-minted session token works: one more turn
  await send(page, ui, "Dopo il ricaricamento.");
  await expect.poll(() => botReplies(request, t)).toBe(2);
  await expect(ui.locator("#composer")).toBeEnabled();

  // a parent that does send the sid: no /state lookup
  server = await serverSession(request, t);
  await page.goto("about:blank");
  api = apiLog(page);
  await page.goto(hostUrl(t, { mode: "resume", sid: server.session_id! }));
  ui = await uiFrame(page);
  await expect(ui.locator("article.turn")).toHaveCount(server.turns.length);
  expect(api[0]).toBe(`GET /api/v1/assess/sessions/${server.session_id}`);
  expect(api).not.toContain("GET /api/v1/assess/state");
});

test("an unanswered download-token request ends in a generic text, asked once", async ({
  page,
  request,
}) => {
  const t: Target = { who: "stu1", a: "wf" };
  await page.goto(hostUrl(t));
  const ui = await uiFrame(page);
  await opened(request, ui, t);
  await tabTo(page, ui, "button.finish");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(ui.locator("#closing-title")).toBeVisible();
  await tabTo(page, ui, ".closing button");
  await page.keyboard.press("Enter");
  const requests = async () =>
    (await fromUi(page)).filter((m) => m === "vektra-assess-download-token-request").length;
  await expect.poll(requests).toBe(1);
  await expect(ui.locator(".closing button")).toBeDisabled();
  // the stub plugin never answers (its download part does not exist yet)
  await expect(ui.locator(".download-status")).toHaveText(TEXT.downloadFailed, { timeout: 15_000 });
  await expect(ui.locator(".closing button")).toBeEnabled();
  await page.waitForTimeout(2000);
  expect(await requests()).toBe(1); // no automatic retry
});

test("download returns the stored transcript bytes", async ({ page, request }) => {
  // REQ-072 / REQ-088 (plan 10): the closing-screen download returns bytes whose SHA-256 equals
  // the stored transcript hash. The stub plugin answers the UI's request with a download token it
  // minted server-side; the UI fetches with the token in the Authorization header only.
  const t: Target = { who: "stu1", a: "files" };
  await page.goto(hostUrl(t, { download: "1" }));
  const ui = await uiFrame(page);
  await opened(request, ui, t);
  await tabTo(page, ui, "button.finish");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(ui.locator("#closing-title")).toBeVisible();
  // the worker generates it after the close: wait for the row, never for a status text
  await expect.poll(async () => (await serverSession(request, t)).transcript_sha256, { timeout: 20_000 })
    .toBeTruthy();
  const server = await serverSession(request, t);
  const fetched: Request[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/assess/transcripts/")) fetched.push(r);
  });
  await tabTo(page, ui, ".closing button");
  const downloading = page.waitForEvent("download");
  await page.keyboard.press("Enter");
  const file = await downloading;
  expect(file.suggestedFilename()).toBe(server.transcript_filename);
  const path = await file.path();
  const bytes = readFileSync(path);
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(server.transcript_sha256);
  // the token travelled in the header, never in the URL
  expect(fetched.length).toBe(1);
  expect(fetched[0].url()).not.toContain("token");
  expect((await fetched[0].allHeaders())["authorization"]).toMatch(/^Bearer /);
  await expect(ui.locator(".download-status")).not.toHaveText(TEXT.downloadFailed);
});
