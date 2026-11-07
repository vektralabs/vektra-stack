// NFR-016 (WCAG 2.1 AA) on the UI content, tested here because the plugin cannot run axe inside
// the cross-origin iframe: axe on every state, reflow at 320 px and at 1280 px / 200% zoom.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Frame, type Page } from "@playwright/test";
import {
  botReplies,
  control,
  hostUrl,
  opened,
  send,
  tabTo,
  TEXT,
  uiFrame,
  type Target,
} from "./helpers";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

/** Every state the plan lists, plus the unavailable text and the flag dialog. */
const STATES = [
  "awaiting",
  "unavailable",
  "preparing",
  "active",
  "streaming",
  "outage",
  "confirm dialog",
  "flag dialog",
  "closed",
] as const;

async function scan(page: Page, state: (typeof STATES)[number], scanned: string[]): Promise<void> {
  // the host page and the cross-origin UI frame alike (axe enters the frame)
  const result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const found = result.violations.map(
    (v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`,
  );
  expect(found, `axe violations in state "${state}"`).toEqual([]);
  // the UI frame was really analysed (not an empty or blocked frame): nodes inside an iframe
  // carry a frame path, so their target has more than one selector
  const inFrame = [...result.passes, ...result.incomplete].some((r) =>
    r.nodes.some((n) => n.target.length > 1),
  );
  expect(inFrame, `the UI frame was not analysed in state "${state}"`).toBe(true);
  scanned.push(state);
}

test("axe: 0 WCAG 2.1 A/AA violations in every state", async ({ page, request }) => {
  const t: Target = { who: "stu1", a: "simple" };
  const scanned: string[] = [];

  await page.goto(hostUrl(t, { hold: "1" }));
  let ui = await uiFrame(page);
  await expect(ui.locator("#status")).toHaveText(TEXT.waiting);
  await scan(page, "awaiting", scanned);

  await page.goto(hostUrl(t, { unavailable: "1" }));
  ui = await uiFrame(page);
  await expect(ui.locator("#status")).toHaveText(TEXT.unavailable);
  await scan(page, "unavailable", scanned);

  await control(request, "pause", { on: true });
  try {
    await page.goto(hostUrl(t));
    ui = await uiFrame(page);
    await expect(ui.locator("#status")).toHaveText(TEXT.preparing);
    await scan(page, "preparing", scanned);
  } finally {
    await control(request, "pause", { on: false });
  }
  await opened(request, ui, t);
  await scan(page, "active", scanned);

  await control(request, "hold", { on: true });
  try {
    await send(page, ui, "Una domanda sul criterio.");
    await expect(ui.locator("li.streaming")).toBeVisible();
    await scan(page, "streaming", scanned);
  } finally {
    await control(request, "hold", { on: false });
  }
  await expect(ui.locator("li.streaming")).toHaveCount(0);

  await control(request, "outage", { on: true });
  try {
    await send(page, ui, "E poi?");
    await expect(ui.locator(".banner")).toContainText(TEXT.outage);
    await scan(page, "outage", scanned);
  } finally {
    await control(request, "outage", { on: false });
  }
  await expect(ui.locator(".banner")).toContainText(TEXT.outageEnd, { timeout: 30_000 });
  await expect(ui.locator("#composer")).toBeEnabled({ timeout: 30_000 });

  await tabTo(page, ui, "button.finish");
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toBeVisible();
  await scan(page, "confirm dialog", scanned);
  await page.keyboard.press("Escape"); // Escape cancels like Annulla
  await expect(ui.locator("dialog[open]")).toHaveCount(0);

  await tabTo(page, ui, "button.flag");
  await page.keyboard.press("Enter");
  await expect(ui.locator("dialog[open]")).toBeVisible();
  await scan(page, "flag dialog", scanned);
  await page.keyboard.press("Escape");

  await tabTo(page, ui, "button.finish");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(ui.locator("#closing-title")).toBeVisible();
  await scan(page, "closed", scanned);

  // false-pass guard: every declared state was scanned
  expect(scanned).toEqual([...STATES]);
});

async function noHorizontalScroll(frame: Frame): Promise<void> {
  const [scroll, client] = await frame.evaluate(() => [
    document.documentElement.scrollWidth,
    document.documentElement.clientWidth,
  ]);
  expect(scroll, `scrollWidth ${scroll} > clientWidth ${client}`).toBeLessThanOrEqual(client);
}

test("reflow: no horizontal scroll at 320 px and at 1280 px with 200% zoom", async ({
  page,
  request,
}) => {
  const t: Target = { who: "stu1", a: "rubric_hidden" };
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto(hostUrl(t));
  const ui = await uiFrame(page);
  await opened(request, ui, t);
  await send(page, ui, "Una frase lunga con unaparolamoltolungasenzaspazicheandrebbeacapo".repeat(2));
  await expect.poll(() => botReplies(request, t), { timeout: 30_000 }).toBe(1);
  await expect(ui.locator("#composer")).toBeEnabled();
  for (const width of [320, 640]) {
    // 1280 CSS px at 200% zoom lays out like a 640 px wide viewport
    await page.setViewportSize({ width, height: 800 });
    await noHorizontalScroll(ui);
    await tabTo(page, ui, "button.finish");
    await page.keyboard.press("Enter");
    await expect(ui.locator("dialog[open]")).toBeVisible();
    await noHorizontalScroll(ui);
    await page.keyboard.press("Escape");
    await expect(ui.locator("dialog[open]")).toHaveCount(0);
  }
});
