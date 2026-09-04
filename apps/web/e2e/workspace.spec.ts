import { expect, test } from "@playwright/test";
import {
  STORY, createProject, gotoTab, jobsSettle, signIn, whenEnabled,
} from "./fixtures";

/**
 * One long test rather than several: the pipeline is inherently sequential and
 * each stage needs the last, so splitting it would mean re-running everything
 * before it. Assertions are labelled so a failure still says which stage broke.
 */
test("story to film, through the interface", async ({ page }) => {
  test.setTimeout(600_000);
  await signIn(page);
  await createProject(page, `E2E ${Date.now()}`);

  await test.step("write and save the story", async () => {
    await page.getByPlaceholder("Paste the story here.").fill(STORY);
    await expect(page.getByText(/\d+ words/)).toBeVisible();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText(/saved v1/)).toBeVisible();
  });

  await test.step("analyse", async () => {
    await (await whenEnabled(page, "Analyse")).click();
    await jobsSettle(page);
  });

  await test.step("generate and apply a storyboard", async () => {
    // Wait on the button, not the queue: a job finishing and the page knowing
    // about it are two different events.
    await (await whenEnabled(page, "Generate")).click();
    await jobsSettle(page);
    await (await whenEnabled(page, /Apply v\d+ to the workspace/)).click();
    await expect(page.getByText(/\d+ scenes ·/)).toBeVisible();
  });

  await test.step("the cast carries a frozen canon", async () => {
    await gotoTab(page, "cast");
    await expect(page.locator(".canon").first()).not.toBeEmpty();
    // A voice picker exists so quoted lines can speak in their own voice.
    await expect(page.locator("select").first()).toBeVisible();
  });

  await test.step("generate a still and approve a candidate", async () => {
    await gotoTab(page, "stills");
    await expect(page.locator(".shotcard").first()).toBeVisible();
    await page.locator(".shotcard").first()
      .getByRole("button", { name: /Generate|Regenerate/ }).click();
    await jobsSettle(page);
    await expect(page.locator(".shotcard").first().locator("img")).toBeVisible();
  });

  await test.step("the prompt inspector shows where each phrase came from", async () => {
    await page.locator(".shotcard").first()
      .getByRole("button", { name: "prompt" }).click();
    const inspector = page.locator(".inspector");
    await expect(inspector).toBeVisible();
    await expect(inspector.locator(".legend .chip").first()).toBeVisible();
    await inspector.getByRole("button", { name: "close" }).click();
  });

  await test.step("editing a shot marks its still stale", async () => {
    const card = page.locator(".shotcard").first();
    await card.getByRole("button", { name: "edit" }).click();
    await card.locator("textarea").fill("A completely different moment");
    await card.getByRole("button", { name: "Save shot" }).click();
    // The picture no longer matches the words that produced it, and says so.
    await expect(card.locator(".badge", { hasText: "stale" })).toBeVisible();
  });
});
