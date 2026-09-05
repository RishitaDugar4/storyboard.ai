import { type Page, expect } from "@playwright/test";

export const EMAIL = process.env.E2E_EMAIL ?? "rishita@local";
export const PASS = process.env.E2E_PASS ?? "beacon-nectar-fern-garnet";

export const STORY =
  "A keeper kept a light for forty years. One winter night the power failed " +
  "and she turned the lens by hand until dawn, and eleven men came home who " +
  "otherwise would not have.";

export async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByPlaceholder("you@local").fill(EMAIL);
  await page.getByPlaceholder("Passphrase").fill(PASS);
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page).toHaveURL(/\/projects$/);
}

export async function createProject(page: Page, title: string) {
  await page.getByPlaceholder("New project title").fill(title);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText(title)).toBeVisible();
  await page.getByText(title).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

/** Wait for the job drawer to go quiet.
 *
 * Necessary but NOT sufficient: a job finishing is not the same as the page
 * having caught up with it. Where a later step depends on new state, wait for
 * that state -- a button enabling, an element appearing -- rather than for the
 * queue to fall silent.
 */
export async function jobsSettle(page: Page, timeout = 90_000) {
  const drawer = page.locator(".jobs");
  const deadline = Date.now() + timeout;
  // Give a job a moment to appear, or "quiet" may just mean "not started yet".
  await page.waitForTimeout(1200);
  while (Date.now() < deadline) {
    // From the drawer's own count, not from the rows it happens to be
    // showing: a minimized drawer renders no rows, and counting those would
    // make "collapsed" indistinguishable from "finished".
    const attr = await drawer.getAttribute("data-active");
    if (attr === null) return;          // drawer gone: no jobs at all
    if (Number(attr) === 0) return;
    await page.waitForTimeout(1500);
  }
  throw new Error("jobs did not settle in time");
}

/** Wait until a button is genuinely clickable, whatever the queue is doing. */
export async function whenEnabled(page: Page, name: string | RegExp,
                                  timeout = 180_000) {
  const button = page.getByRole("button", { name }).first();
  await expect(button).toBeEnabled({ timeout });
  return button;
}

/**
 * Workspace tabs by test id, not by name: the labels carry counts ("cast (4)")
 * and the storyboard panel has tabs of its own with the same words.
 */
export async function gotoTab(
  page: Page,
  tab: "story" | "cast" | "stills" | "film" | "settings",
): Promise<void> {
  await page.getByTestId(`tab-${tab}`).click();
}
