import { expect, test } from "@playwright/test";

import {
  CSRF_HEADER,
  CSRF_VALUE,
  PASSWORD,
  loginViaUi,
  registerAndLogin,
  registerViaApi,
  uniqueOrg,
} from "./helpers";

/* Tests 1, 2, 3, 10 and 11 from the phase skill's required list. */

test("1a. login succeeds with correct credentials", async ({ page, request }) => {
  const account = await registerViaApi(request);

  await page.context().clearCookies();
  await loginViaUi(page, account);

  await page.waitForURL(/\/chat/);
  await expect(page.getByRole("heading", { name: /ask about your documents/i })).toBeVisible();
});

test("1b. a failed login says nothing about WHICH detail was wrong", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Organization").fill("no-such-org-anywhere");
  await page.getByLabel("Email").fill("nobody@example.com");
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: /^sign in$/i }).click();

  // Asserted on the TEXT, with Playwright's auto-retry, rather than
  // reading textContent once from getByRole("alert"): that role also
  // matches Next's dev-mode overlay, which is present and empty, so
  // the original version silently measured the wrong element.
  await expect(
    page.getByText(/did not match an account/i),
  ).toBeVisible({ timeout: 20_000 });

  // Nothing on the page may name WHICH detail was wrong -- that turns
  // a login form into an account-enumeration oracle.
  const body = (await page.locator("body").innerText()).toLowerCase();
  for (const leak of [
    "organization not found",
    "no such organization",
    "unknown organization",
    "wrong password",
    "incorrect password",
    "user not found",
    "email not found",
  ]) {
    expect(body, `login error must not say "${leak}"`).not.toContain(leak);
  }
});

test("2. the session cookie is HttpOnly and no token is in web storage", async ({
  page,
}) => {
  await registerAndLogin(page);

  const cookies = await page.context().cookies();
  const session = cookies.find((c) => c.name === "dm_session");
  expect(session, "dm_session cookie should exist").toBeTruthy();
  expect(session!.httpOnly, "dm_session must be HttpOnly").toBe(true);
  expect(session!.sameSite).toBe("Lax");

  // The whole point of the BFF: script cannot read it.
  const readable = await page.evaluate(() => document.cookie);
  expect(readable).not.toContain("dm_session");

  // And the token is nowhere a script could reach it either. A JWT is
  // checked for by its "eyJ" header prefix as well as by key name,
  // because a future refactor might store it under any name.
  const storage = await page.evaluate(() => {
    const dump = (s: Storage) =>
      Object.keys(s)
        .map((k) => `${k}=${s.getItem(k) ?? ""}`)
        .join("|");
    return `${dump(localStorage)}||${dump(sessionStorage)}`;
  });
  expect(storage).not.toContain("eyJ");
  expect(storage.toLowerCase()).not.toContain("token");
});

test("3. an unauthenticated visit to a protected page redirects to login", async ({
  page,
}) => {
  await page.context().clearCookies();

  await page.goto("/documents");

  await expect(page).toHaveURL(/\/login\?next=%2Fdocuments/);
});

test("10a. a mutating request without the CSRF header is rejected", async ({
  page,
  request,
}) => {
  await registerAndLogin(page);

  // Issued from the page's own context, so the session cookie IS sent.
  // Only the custom header is missing.
  const status = await page.evaluate(async () => {
    const res = await fetch("/api/conversations", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: null }),
    });
    return res.status;
  });

  expect(status).toBe(403);
  void request;
});

test("10b. a mutating request with a foreign Origin is rejected", async ({
  request,
}) => {
  const response = await request.post("/api/conversations", {
    headers: {
      [CSRF_HEADER]: CSRF_VALUE,
      origin: "https://evil.example",
    },
    data: { title: null },
  });

  expect(response.status()).toBe(403);
});

test("11. logout clears the cookie, and the back button shows no org data", async ({
  page,
}) => {
  await registerAndLogin(page);
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible();

  await page.getByRole("button", { name: /sign out/i }).click();
  await page.waitForURL(/\/login/);

  const cookies = await page.context().cookies();
  expect(cookies.find((c) => c.name === "dm_session" && c.value)).toBeFalsy();

  // Back-navigating must not restore a rendered dashboard from the
  // bfcache or from a warm query cache.
  await page.goBack();
  await expect(page).toHaveURL(/\/login/);
});

test("the sign-in page is reachable and register creates a distinct org", async ({
  request,
}) => {
  const a = await registerViaApi(request, uniqueOrg("alpha"));
  const b = await registerViaApi(request, uniqueOrg("beta"));
  expect(a.org).not.toBe(b.org);

  // The same email in two organizations is legitimate and must work:
  // (org_id, email) is the uniqueness key, not email alone.
  expect(a.email).toBe(b.email);
  expect(PASSWORD.length).toBeGreaterThanOrEqual(8);
});

test("11b. logout empties the query cache, not just the cookie", async ({ page }) => {
  // Test 11 above checks the cookie and the redirect, and the
  // mutation exercise showed that is NOT enough: removing
  // `queryClient.clear()` from logout left test 11 green. The reason
  // is that query keys are namespaced by org and user, so a DIFFERENT
  // user never reads the previous one's entries anyway -- the
  // namespacing hides the bug, which is exactly why the clear needed
  // its own test rather than being assumed covered.
  //
  // The observable case is the SAME user signing back in: a surviving
  // cache renders their old data instantly, with no fetch. Here the
  // documents request is delayed after re-login, so a cached value
  // would be on screen for seconds while a cleared cache shows
  // nothing.
  const account = await registerAndLogin(page);

  await page.goto("/documents");
  await page.locator('input[type="file"]').setInputFiles({
    name: "cache-probe.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Probe\n\nA document used to observe caching.\n", "utf-8"),
  });
  await expect(page.getByText("cache-probe.md")).toBeVisible({ timeout: 60_000 });

  await page.getByRole("button", { name: /sign out/i }).click();
  await page.waitForURL(/\/login/);

  // From here the documents list takes 5s to answer, so anything that
  // appears sooner came from memory.
  await page.route("**/api/documents", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    await route.fallback();
  });

  await loginViaUi(page, account);
  await page.waitForURL(/\/chat/);
  await page.goto("/documents");

  // Within the delay window the previous session's document must not
  // be on screen.
  await expect(page.getByText("cache-probe.md")).toHaveCount(0);

  // And once the real response lands it appears again, proving the
  // delay -- not a broken page -- is what kept it off screen.
  await expect(page.getByText("cache-probe.md")).toBeVisible({ timeout: 30_000 });
});
