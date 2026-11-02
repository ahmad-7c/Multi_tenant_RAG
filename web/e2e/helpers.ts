import type { APIRequestContext, Page } from "@playwright/test";

/**
 * Shared setup for the end-to-end suite.
 *
 * Every test creates its OWN organization. Tenant isolation is the
 * property most of these tests exist to check, so sharing one org
 * between them would mean a leak could pass unnoticed -- and a test
 * that deletes a document would break whichever test ran next.
 */

export const CSRF_HEADER = "x-documind-csrf";
export const CSRF_VALUE = "1";

/** A password that satisfies the backend's 8-character minimum. */
export const PASSWORD = "e2e-password-1";

let counter = 0;

export function uniqueOrg(prefix = "e2e"): string {
  counter += 1;
  // Time plus a counter: two orgs created in the same millisecond
  // would otherwise collide on the unique name constraint.
  return `${prefix}-${Date.now()}-${counter}`;
}

export type Account = { org: string; email: string; password: string };

/** Register a fresh organization through the BFF and sign in. */
export async function registerAndLogin(
  page: Page,
  options: { org?: string } = {},
): Promise<Account> {
  const org = options.org ?? uniqueOrg();
  const email = `admin@example.com`;

  await page.goto("/register");
  await page.getByLabel("Organization").fill(org);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: /create organization/i }).click();
  await page.waitForURL(/\/chat/);

  return { org, email, password: PASSWORD };
}

/** Register via the API, without driving the UI. */
export async function registerViaApi(
  request: APIRequestContext,
  org = uniqueOrg(),
): Promise<Account> {
  const email = "admin@example.com";
  const response = await request.post("/api/auth/register", {
    // Origin as well as the CSRF header: the BFF rejects a mutating
    // request with no Origin, because a same-origin fetch from the app
    // always sends one. Supplying it here is the helper behaving like
    // the browser, not a weakening of the check -- test 10b asserts a
    // FOREIGN origin is still refused.
    headers: {
      [CSRF_HEADER]: CSRF_VALUE,
      origin: "http://localhost:3020",
    },
    data: { org_name: org, email, password: PASSWORD },
  });
  if (!response.ok()) {
    throw new Error(`register failed: ${response.status()} ${await response.text()}`);
  }
  return { org, email, password: PASSWORD };
}

export async function loginViaUi(page: Page, account: Account) {
  await page.goto("/login");
  await page.getByLabel("Organization").fill(account.org);
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password").fill(account.password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
}

/** Upload a document and wait for it to reach a terminal state. */
export async function uploadDocument(
  page: Page,
  name: string,
  body: string,
): Promise<void> {
  await page.goto("/documents");

  // Wait for the client to be LIVE before touching the file input.
  // `setInputFiles` fires a change event whether or not React has
  // hydrated, and a change event with no listener is silently
  // discarded -- the upload simply never happens and the test then
  // waits a minute for a row that was never created. This text is
  // rendered from a RESOLVED client query, so its presence proves
  // both hydration and a completed first fetch.
  await page
    .getByText(/nothing uploaded yet|documents? in this organization/i)
    .first()
    .waitFor({ timeout: 30_000 });

  await page.locator('input[type="file"]').setInputFiles({
    name,
    mimeType: "text/markdown",
    buffer: Buffer.from(body, "utf-8"),
  });
  // Ingestion runs inline in the e2e stack, so "ready" arrives on the
  // next poll rather than whenever a worker gets to it.
  await page
    .getByRole("row", { name: new RegExp(escapeRegExp(name)) })
    .getByText(/ready|failed/i)
    .first()
    .waitFor({ timeout: 60_000 });
}

export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export const POLICY = `# Remote Work Policy

## Core hours
Remote employees must be reachable between 10:00 and 16:00 in the
timezone of their assigned office.

## Equipment
Requests for additional equipment above 400 USD require director
approval before purchase.
`;
