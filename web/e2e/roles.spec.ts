import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { PASSWORD, loginViaUi, registerAndLogin, uniqueOrg } from "./helpers";

/**
 * Test 4: a member sees no admin controls, and cannot reach /usage.
 *
 * Registration always creates the organization's FIRST ADMIN and
 * there is no invite endpoint yet, so a member cannot be produced
 * through the product. The fixture writes the row directly rather
 * than adding an unused invite API purely to make a test possible.
 */

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = path.join(REPO_ROOT, "venv", "Scripts", "python.exe");

function makeMember(org: string, email: string) {
  execFileSync(PYTHON, ["scripts/e2e_make_member.py", org, email], {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      DM_MEMBER_PASSWORD: PASSWORD,
      // The fixture must write to the e2e database, not the developer's.
      DATABASE_URL:
        "postgresql://documind:documind@localhost:5434/documind_e2e",
      ENV: "development",
    },
  });
}

test("4. a member sees no admin controls and cannot reach usage", async ({
  page,
}) => {
  const org = uniqueOrg("roles");
  await registerAndLogin(page, { org });

  const memberEmail = "member@example.com";
  makeMember(org, memberEmail);

  // Sign out of the admin session and in as the member.
  await page.getByRole("button", { name: /sign out/i }).click();
  await page.waitForURL(/\/login/);
  await loginViaUi(page, { org, email: memberEmail, password: PASSWORD });
  await page.waitForURL(/\/chat/);

  // The sidebar offers no Usage link.
  await expect(page.getByRole("link", { name: "Usage" })).toHaveCount(0);
  // Documents and Chat remain available: a member can read and ask.
  await expect(page.getByRole("link", { name: "Documents" })).toBeVisible();

  // No upload or delete controls on Documents.
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();
  await expect(page.getByText(/you have read access/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /upload document/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^delete$/i })).toHaveCount(0);

  // Typing the URL does not get them in either -- hiding a link is not
  // access control.
  const response = await page.goto("/usage");
  expect(response?.status()).toBe(404);

  // And the API refuses independently of anything the UI decided.
  const status = await page.evaluate(async () => {
    const res = await fetch("/api/usage?days=7");
    return res.status;
  });
  expect(status).toBe(403);
});
