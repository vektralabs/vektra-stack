// Shared helpers of the e2e specs: the origins, the host page URL, the harness controls, the
// keyboard-only navigation and the live-region counter.
import { expect, type APIRequestContext, type Frame, type Page } from "@playwright/test";

function env(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set: run the specs through tests/integration/test_ui_e2e.py`);
  return v;
}

/** The Moodle stub (the plugin's origin, the site's moodle_origin). */
export const HOST = env("E2E_HOST");
/** Another origin that is not the site's: it must not be able to frame the UI. */
export const THIRD = env("E2E_THIRD");
/** The Vektra origin serving the UI and the API. */
export const UI = env("E2E_UI");

export const TEXT = {
  waiting: "In attesa dell'avvio della discussione…",
  preparing: "Preparazione della discussione…",
  unavailable: "La discussione non è disponibile in questo momento. Riprova più tardi.",
  replyDone: "Risposta dell'assistente ricevuta",
  outage: "Assistente temporaneamente non disponibile",
  outageEnd: "L'assistente è di nuovo disponibile",
  downloadFailed: "Non è stato possibile scaricare la trascrizione. Riprova più tardi.",
};

export interface Target {
  who: string;
  a: string;
}

export function hostUrl(t: Target, extra: Record<string, string> = {}, origin = HOST): string {
  return `${origin}/host?${new URLSearchParams({ who: t.who, a: t.a, ...extra })}`;
}

/** The UI frame of the host page, once its page has booted. */
export async function uiFrame(page: Page): Promise<Frame> {
  let frame: Frame | undefined;
  await expect
    .poll(() => (frame = page.frames().find((f) => f.url().startsWith(`${UI}/api/v1/assess/ui`))))
    .toBeTruthy();
  await frame!.waitForSelector("main#app h1");
  return frame!;
}

export interface ServerTurn {
  turn_no: number;
  role: string;
  kind: string;
  text: string;
  flagged: boolean;
}

export interface ServerSession {
  session_id: string | null;
  state: string;
  close_reason: string | null;
  deadline_at: string | null;
  transcript_jobs: number;
  transcript_sha256: string | null; // plan 10: the stored transcript's SHA-256, once generated
  transcript_filename: string | null;
  turns: ServerTurn[];
}

/** Server state (database rows and GET /sessions/{sid}), never what the UI shows. */
export async function serverSession(request: APIRequestContext, t: Target): Promise<ServerSession> {
  const r = await request.get(`${HOST}/control/session?${new URLSearchParams({ ...t })}`);
  expect(r.ok()).toBeTruthy();
  return (await r.json()) as ServerSession;
}

export async function control(
  request: APIRequestContext,
  name: "pause" | "outage" | "hold" | "warn",
  body: Record<string, unknown> = {},
  t?: Target,
): Promise<void> {
  const q = t ? `?${new URLSearchParams({ ...t })}` : "";
  const r = await request.post(`${HOST}/control/${name}${q}`, { data: body });
  expect(r.ok()).toBeTruthy();
}

/** Keyboard only: press Tab until ``selector`` inside the UI frame has the focus. */
export async function tabTo(page: Page, frame: Frame, selector: string, max = 80): Promise<void> {
  for (let i = 0; i < max; i++) {
    const there = await frame.evaluate(
      (s) => document.hasFocus() && !!document.activeElement?.matches(s),
      selector,
    );
    if (there) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`not reachable with Tab: ${selector}`);
}

/** Keyboard only: type a message and send it with Invia. */
export async function send(page: Page, frame: Frame, text: string): Promise<void> {
  await expect(frame.locator("#composer")).toBeEnabled();
  await tabTo(page, frame, "#composer");
  await page.keyboard.type(text);
  await tabTo(page, frame, "form.composer button.primary");
  await page.keyboard.press("Enter");
}

/** Count what the polite live region announces from now on (one added text node per say). */
export async function watchAnnouncements(frame: Frame): Promise<void> {
  await frame.evaluate(() => {
    const w = window as unknown as { __said?: string[] };
    if (w.__said) return;
    w.__said = [];
    const region = document.querySelector('[aria-live="polite"].sr-only');
    if (!region) throw new Error("no live region");
    new MutationObserver((records) => {
      for (const r of records)
        r.addedNodes.forEach((n) => n.textContent && w.__said!.push(n.textContent));
    }).observe(region, { childList: true });
  });
}

export async function said(frame: Frame, part: string): Promise<number> {
  const all = await frame.evaluate(() => (window as unknown as { __said: string[] }).__said);
  return all.filter((s) => s.includes(part)).length;
}

/** The messages the UI posted to the host page (types only), as the host recorded them. */
export async function fromUi(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __fromUi: string[] }).__fromUi);
}

/** Wait for the session to be ACTIVE with its opening rendered. */
export async function opened(request: APIRequestContext, frame: Frame, t: Target): Promise<void> {
  await expect(frame.locator("article.turn-bot").first()).toBeVisible({ timeout: 60_000 });
  await expect.poll(async () => (await serverSession(request, t)).state).toBe("ACTIVE");
}

/** The number of completed bot replies (opening excluded) the server holds. */
export async function botReplies(request: APIRequestContext, t: Target): Promise<number> {
  const s = await serverSession(request, t);
  return s.turns.filter((x) => x.role === "bot" && x.kind !== "opening").length;
}
