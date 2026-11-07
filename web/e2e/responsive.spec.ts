import { expect, test } from "@playwright/test";

import { POLICY, registerAndLogin, uploadDocument } from "./helpers";

/**
 * The three widths the design spec names, with the one behaviour that
 * only matters on a phone: the sidebar must be dismissable.
 *
 * On desktop the 256px rail is permanent. Below `lg` it becomes a
 * slide-over, which means there has to be a way to open it AND a way
 * to get rid of it -- a drawer covering 70% of a 390px screen with no
 * exit is worse than no drawer.
 */

const PHONE = { width: 390, height: 844 };
const TABLET = { width: 1024, height: 768 };
const DESKTOP = { width: 1440, height: 900 };

/** No page-level horizontal scroll. Wide content scrolls in its own box. */
async function expectNoHorizontalScroll(
  page: import("@playwright/test").Page,
  where: string,
) {
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(
    overflow.scrollWidth,
    `${where} scrolls horizontally (${overflow.scrollWidth} > ${overflow.clientWidth})`,
  ).toBeLessThanOrEqual(overflow.clientWidth + 1);
}

test("the sidebar opens and closes on a phone", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await registerAndLogin(page);
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();

  // The rail is NOT taking space on a phone.
  await expect(page.getByRole("link", { name: "Documents" })).toHaveCount(0);

  // Open it.
  const menu = page.getByRole("button", { name: /open menu/i });
  await expect(menu).toBeVisible();
  await menu.click();
  await expect(page.getByRole("link", { name: "Documents" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Chat" })).toBeVisible();

  // Close it again -- the part that makes it usable.
  await page.getByRole("button", { name: /close menu/i }).click();
  await expect(page.getByRole("link", { name: "Documents" })).toHaveCount(0);

  // And navigating from it closes it too, rather than leaving the
  // drawer over the page it just opened.
  await menu.click();
  await page.getByRole("link", { name: "Chat" }).click();
  await page.waitForURL(/\/chat/);
  await expect(page.getByRole("link", { name: "Chat" })).toHaveCount(0);
});

test("no horizontal scroll on any screen at any width", async ({ page }) => {
  await page.setViewportSize(DESKTOP);
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  for (const [label, size] of [
    ["390", PHONE],
    ["1024", TABLET],
    ["1440", DESKTOP],
  ] as const) {
    await page.setViewportSize(size);
    for (const path of ["/", "/how-it-works", "/how-its-built", "/login"]) {
      await page.goto(path);
      await expect(page.locator("h1, h2").first()).toBeVisible();
      await expectNoHorizontalScroll(page, `${path} @ ${label}`);
    }
  }
});

test("the signed-in screens do not overflow on a phone", async ({ page }) => {
  await page.setViewportSize(PHONE);
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  for (const [path, ready] of [
    ["/documents", /Documents/],
    ["/chat", /Ask about your documents/],
    ["/usage", /Usage/],
  ] as const) {
    await page.goto(path);
    await expect(page.getByText(ready).first()).toBeVisible();
    await expectNoHorizontalScroll(page, `${path} @ 390`);
  }
});
