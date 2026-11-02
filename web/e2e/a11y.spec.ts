import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { POLICY, registerAndLogin, uploadDocument } from "./helpers";

/**
 * Test 13: an axe scan on every screen, zero serious or critical.
 *
 * Only `serious` and `critical` fail the build. `minor` and
 * `moderate` are reported but not gated, because axe counts things
 * like a decorative element's contrast among them and a gate that
 * fires on those gets disabled within a week -- at which point it
 * catches nothing at all.
 */

const SEVERITIES = ["serious", "critical"] as const;

/**
 * Scans run with REDUCED MOTION emulated.
 *
 * Not a convenience. `Reveal` holds its children at opacity-0 until
 * they scroll into view, and axe computes the contrast of a
 * transparent element against whatever is behind it -- which flagged
 * a pure-white heading as a contrast failure. Reduced motion makes
 * Reveal render its resting state immediately, so the scan measures
 * the colours a reader actually sees, and it checks the
 * reduced-motion rendering at the same time.
 */
async function scan(page: import("@playwright/test").Page, label: string) {
  const results = await new AxeBuilder({ page })
    // WCAG 2.1 AA, which is the bar the design spec sets.
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    // The Next.js dev overlay is injected into every page in dev and
    // is not ours to fix; excluding it keeps the signal about our own
    // markup.
    .exclude("nextjs-portal")
    .analyze();

  const blocking = results.violations.filter((v) =>
    (SEVERITIES as readonly string[]).includes(v.impact ?? ""),
  );

  if (blocking.length > 0) {
    const detail = blocking
      .map(
        (v) =>
          `  [${v.impact}] ${v.id}: ${v.help}\n` +
          v.nodes.slice(0, 3).map((n) => `      ${n.target.join(" ")}`).join("\n"),
      )
      .join("\n");
    throw new Error(`${label} has ${blocking.length} blocking violation(s):\n${detail}`);
  }

  // Lesser findings are printed, not failed -- visible without being
  // a gate that gets switched off.
  const lesser = results.violations.filter(
    (v) => !(SEVERITIES as readonly string[]).includes(v.impact ?? ""),
  );
  if (lesser.length > 0) {
    console.log(
      `${label}: ${lesser.length} non-blocking (${lesser.map((v) => `${v.impact}/${v.id}`).join(", ")})`,
    );
  }
}

test.describe("public pages", () => {
  for (const [label, path] of [
    ["landing", "/"],
    ["how-it-works", "/how-it-works"],
    ["how-its-built", "/how-its-built"],
    ["login", "/login"],
    ["register", "/register"],
    ["design reference", "/design"],
  ] as const) {
    test(`13. axe: ${label}`, async ({ page }) => {
      await page.context().clearCookies();
      const response = await (async () => {
        await page.emulateMedia({ reducedMotion: "reduce" });
        return page.goto(path);
      })();
      // /design 404s in a production build by design -- it renders
      // sample data and must never ship. Skipped rather than failed:
      // its absence is the correct behaviour being asserted elsewhere.
      test.skip(
        response?.status() === 404,
        `${path} is not served in this build`,
      );
      // Wait for the first heading so the scan runs on a painted page
      // rather than a skeleton.
      await expect(page.locator("h1, h2").first()).toBeVisible();
      await scan(page, label);
    });
  }
});

test.describe("signed-in pages", () => {
  // This one test registers, uploads, ingests, asks a question and
  // runs four separate scans, all against a dev server compiling
  // routes on first visit. The default budget is for a single
  // interaction, not a whole session.
  test.setTimeout(240_000);

  test("13. axe: documents, chat and usage", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await registerAndLogin(page);
    await uploadDocument(page, "remote-work-policy.md", POLICY);

    await page.goto("/documents");
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();
    await scan(page, "documents");

    await page.goto("/chat");
    await expect(page.getByPlaceholder("Ask about your documents")).toBeVisible();
    await scan(page, "chat (empty)");

    // Scanned WITH an answer on screen: the citation chips, trace chips
    // and sources panel only exist in that state, and they are the most
    // custom markup in the product.
    await page
      .getByPlaceholder("Ask about your documents")
      .fill("What are the core hours for remote employees?");
    await page.getByRole("button", { name: /^ask$/i }).click();
    await expect(page.getByText(/10:00/).first()).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: /show source 1/i }).first().click();
    await scan(page, "chat (answered, sources open)");

    await page.goto("/usage");
    await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
    await scan(page, "usage");
  });
});
