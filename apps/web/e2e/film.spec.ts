import { expect, test } from "@playwright/test";
import {
  STORY, createProject, gotoTab, jobsSettle, signIn, whenEnabled,
} from "./fixtures";

/** Narration, music, rendering, and the render-history chips. */
test("narration, music and repeated renders", async ({ page }) => {
  test.setTimeout(900_000);
  await signIn(page);
  await createProject(page, `E2E film ${Date.now()}`);

  await page.getByPlaceholder("Paste the story here.").fill(STORY);
  await page.getByRole("button", { name: "Save" }).click();
  await (await whenEnabled(page, "Analyse")).click();
  await jobsSettle(page);
  await (await whenEnabled(page, "Generate")).click();
  await jobsSettle(page);
  await (await whenEnabled(page, /Apply v\d+/)).click();
  // Applying materialises scenes and shots; counting before that lands gives
  // zero cards, because count() does not wait the way an assertion does.
  await expect(page.getByText(/\d+ scenes ·/)).toBeVisible();

  await test.step("every shot gets a still", async () => {
    await gotoTab(page, "stills");
    const cards = page.locator(".shotcard");
    await expect(cards.first()).toBeVisible();
    const n = await cards.count();
    for (let i = 0; i < n; i++) {
      await cards.nth(i).getByRole("button", { name: /Generate|Regenerate/ }).click();
      await page.waitForTimeout(200);
    }
    await jobsSettle(page, 180_000);
  });

  await test.step("music can be attached", async () => {
    await gotoTab(page, "film");
    const music = page.getByTestId("music");
    await expect(music).toContainText("no music bed");
    // A tiny but valid WAV, so the upload path is exercised for real.
    const pcm = Buffer.alloc(48_000 * 2);
    const header = Buffer.alloc(44);
    header.write("RIFF", 0); header.writeUInt32LE(36 + pcm.length, 4);
    header.write("WAVE", 8); header.write("fmt ", 12);
    header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20);
    header.writeUInt16LE(1, 22); header.writeUInt32LE(24000, 24);
    header.writeUInt32LE(48000, 28); header.writeUInt16LE(2, 32);
    header.writeUInt16LE(16, 34); header.write("data", 36);
    header.writeUInt32LE(pcm.length, 40);
    await page.locator('input[type="file"][accept="audio/*"]').setInputFiles({
      name: "bed.wav", mimeType: "audio/wav",
      buffer: Buffer.concat([header, pcm]),
    });
    await expect(music).toContainText("bed.wav");
  });

  await test.step("record narration and measure the fit", async () => {
    await page.getByRole("button", { name: "Record all" }).click();
    await jobsSettle(page, 300_000);
    await expect(page.getByText(/\d+\/\d+ lines recorded/)).toBeVisible();
    // Fit stops being a guess once the audio exists.
    await expect(page.locator(".chip", { hasText: /^(fits|tight|overflow)/ })
      .first()).toBeVisible();
  });

  await test.step("render once and watch it", async () => {
    await page.getByRole("button", { name: "Render preview" }).click();
    await jobsSettle(page, 600_000);
    const player = page.getByTestId("player");
    await expect(player).toBeVisible();
    await expect(player.locator("video")).toBeVisible();
    await expect(player.getByRole("link", { name: /save to your computer/ }))
      .toHaveAttribute("download", /\.mp4$/);
  });

  await test.step("a second render gives a switchable history", async () => {
    await page.getByRole("button", { name: "Render preview" }).click();
    await jobsSettle(page, 600_000);

    const history = page.getByTestId("render-history");
    await expect(history).toBeVisible();
    const chips = history.locator("button");
    expect(await chips.count()).toBeGreaterThanOrEqual(2);

    // Switching chips changes which file the player is showing -- otherwise
    // earlier versions are unreachable even though the files still exist.
    const player = page.getByTestId("player");
    const first = await player.locator("video").getAttribute("src");
    await chips.nth(1).click();
    await expect(player.locator("video")).not.toHaveAttribute("src", first!);
  });
});
