import { defineConfig } from "@playwright/test";

/**
 * These drive the real browser against a running stack, because the bug that
 * shipped — a login form that never sent the email field — was invisible to
 * every API test and to a curl-based smoke run. The thing you click has to be
 * the thing that is checked.
 *
 *   make dev-fake            (in one terminal)
 *   make test-ui             (in another)
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,        // one shared database
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
  },
});
