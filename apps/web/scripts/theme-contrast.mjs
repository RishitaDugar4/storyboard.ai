/**
 * Reads the *rendered* colours out of the running app and checks each
 * foreground/background pair against WCAG AA. Computed styles rather than the
 * stylesheet, so cascade accidents -- a hover rule outranking a filled chip,
 * say -- show up as a number instead of as something to squint at.
 */
import { chromium } from "@playwright/test";

const rgb = (s) => s.match(/\d+/g).slice(0, 3).map(Number);
const lin = (c) => {
  c /= 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const ratio = (a, b) => {
  const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

const PID = process.env.PID ?? "01a06d87-f4a8-766c-acfb-89657877ab84";
const browser = await chromium.launch();
let failures = 0;

for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ colorScheme: scheme });
  const page = await ctx.newPage();
  await page.goto("http://localhost:3100/login");
  await page.getByPlaceholder("you@local").fill("rishita@local");
  await page.getByPlaceholder("Passphrase").fill("beacon-nectar-fern-garnet");
  await page.getByRole("button", { name: "Enter" }).click();
  await page.waitForURL(/\/projects$/);
  await page.goto(`http://localhost:3100/projects/${PID}`);
  await page.waitForTimeout(1500);
  await page.getByTestId("tab-stills").click();
  await page.waitForTimeout(1000);
  // Move the pointer off the tab so hover does not colour the reading.
  await page.mouse.move(5, 5);
  await page.waitForTimeout(200);

  const probe = async (label, sel, min = 4.5) => {
    const el = page.locator(sel).first();
    if (!(await el.count())) return console.log(`  ${label}: (absent)`);
    const { fg, bg } = await el.evaluate((node) => {
      const s = getComputedStyle(node);
      let p = node, back = s.backgroundColor;
      while (back === "rgba(0, 0, 0, 0)" && p.parentElement) {
        p = p.parentElement;
        back = getComputedStyle(p).backgroundColor;
      }
      return { fg: s.color, bg: back };
    });
    const r = ratio(fg, bg);
    const ok = r >= min;
    if (!ok) failures++;
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label.padEnd(26)} ${r.toFixed(2)}:1`
                + ` (needs ${min})  ${fg} on ${bg}`);
  };

  console.log(`\n${scheme}:`);
  await probe("body text", "h2");
  await probe("muted text", ".muted");
  await probe("active tab (filled)", ".chip.primary", 4.5);
  await probe("inactive tab", ".chip:not(.primary)");
  await probe("stepper done pill", "ol.stepper li.done");
  await probe("button label", ".shotcard button");
  await probe("job drawer heading", ".jobs header");
  await ctx.close();
}

await browser.close();
console.log(failures ? `\n${failures} pair(s) below AA` : "\nall pairs pass AA");
process.exit(failures ? 1 : 0);
