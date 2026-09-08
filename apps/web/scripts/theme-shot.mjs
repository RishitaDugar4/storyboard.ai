import { chromium } from "@playwright/test";

const PID = process.env.PID ?? "01a06d87-f4a8-766c-acfb-89657877ab84";
const OUT = process.env.OUT ?? "/tmp";
const browser = await chromium.launch();

for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({
    colorScheme: scheme, viewport: { width: 1240, height: 1000 },
  });
  const page = await ctx.newPage();
  await page.goto("http://localhost:3100/login");
  await page.getByPlaceholder("you@local").fill("rishita@local");
  await page.getByPlaceholder("Passphrase").fill("beacon-nectar-fern-garnet");
  await page.screenshot({ path: `${OUT}/th-1-login-${scheme}.png` });
  await page.getByRole("button", { name: "Enter" }).click();
  await page.waitForURL(/\/projects$/);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/th-2-projects-${scheme}.png` });

  await page.goto(`http://localhost:3100/projects/${PID}`);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/th-3-story-${scheme}.png` });

  await page.getByTestId("tab-stills").click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/th-4-stills-${scheme}.png` });

  // The prompt inspector carries the categorical legend.
  const promptBtn = page.locator(".shotcard").first()
    .getByRole("button", { name: "prompt" });
  if (await promptBtn.count()) {
    await promptBtn.click();
    await page.waitForTimeout(600);
    await page.locator(".inspector").first()
      .screenshot({ path: `${OUT}/th-5-inspector-${scheme}.png` });
  }

  await page.getByTestId("tab-film").click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/th-6-film-${scheme}.png` });
  await ctx.close();
}

await browser.close();
console.log("captured");
