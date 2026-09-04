import { expect, test } from "@playwright/test";

const EMAIL = process.env.E2E_EMAIL ?? "rishita@local";
const PASS = process.env.E2E_PASS ?? "beacon-nectar-fern-garnet";

test.describe("signing in", () => {
  test("the form asks for both an email and a passphrase", async ({ page }) => {
    // The regression that shipped: a form with only a passphrase field, which
    // the API rejected as invalid and the page reported as "API unreachable".
    await page.goto("/login");
    await expect(page.getByPlaceholder("you@local")).toBeVisible();
    await expect(page.getByPlaceholder("Passphrase")).toBeVisible();
  });

  test("wrong credentials say so, rather than blaming the API", async ({ page }) => {
    await page.goto("/login");
    await page.getByPlaceholder("you@local").fill(EMAIL);
    await page.getByPlaceholder("Passphrase").fill("definitely-not-it");
    await page.getByRole("button", { name: "Enter" }).click();

    await expect(page.locator(".err")).toContainText("do not match an account");
    await expect(page.locator(".err")).not.toContainText("Could not reach");
  });

  test("correct credentials reach the projects page", async ({ page }) => {
    await page.goto("/login");
    await page.getByPlaceholder("you@local").fill(EMAIL);
    await page.getByPlaceholder("Passphrase").fill(PASS);
    await page.getByRole("button", { name: "Enter" }).click();

    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  });

  test("an unauthenticated visitor is sent to the login page", async ({ page }) => {
    await page.context().clearCookies();
    await page.goto("/projects");
    await expect(page).toHaveURL(/\/login$/);
  });
});
