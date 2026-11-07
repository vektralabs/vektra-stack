// Playwright config of the discussion UI e2e (plan 9). The specs need the servers, the fakes and
// the two origins that vektra-assess/tests/integration/test_ui_e2e.py starts: run them through
// that test (make test-assess-e2e), never on their own.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.e2e\.ts$/,
  // one browser, one stack: the specs share the fakes' switches, so they run in order
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: true,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  outputDir: process.env.E2E_OUTPUT ?? "test-results",
  reporter: [["list"], ["json", { outputFile: process.env.E2E_REPORT ?? "e2e-report.json" }]],
  use: {
    browserName: "chromium",
    headless: true,
    viewport: { width: 1024, height: 900 },
    // a failed spec keeps its trace (network, console, DOM snapshots): CI uploads it
    trace: "retain-on-failure",
  },
});
