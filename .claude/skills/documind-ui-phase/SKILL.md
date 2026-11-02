---
name: "documind-ui-phase"
description: "UI phase playbook for DocuMind: Next.js BFF with httpOnly cookie, honest backend data for the UI, streamed chat with citations, security and a11y tests. Checkpointed; pairs with documind-ui-design."
---
 
# DocuMind UI Phase (how it is built)
 
The skill `documind-ui-design` defines WHAT the UI looks and moves like. This skill defines HOW it is built: architecture, backend changes, security, tests, and checkpoints. Use both. On conflict: security and architecture follow this skill; visuals follow documind-ui-design.
 
The human is a learning engineer who must understand every decision. Each feature report includes a 4-line Engineer note (problem, why this solution, how it works, where it lives).
 
## Operating rules
 
1. Work in checkpoints. At each one STOP, report in the format below, and wait for the human.
2. Tests first for every backend change. No behavior change outside the stated scope.
3. Never print, log, commit, or hard-code secrets. Provider keys exist only on the backend. The frontend holds no provider key and no secret in any NEXT_PUBLIC variable.
4. Never show a number the backend did not produce. Anything not backed by a real field is hidden or replaced by an honest alternative (see Data honesty). Never fake a statistic.
5. Do not claim something works unless you ran it. Report what you did NOT verify.
6. The agent that builds does not judge its own output. Use the subagents (below).
7. Verify third-party facts (Vercel limits, library APIs) against current official docs before relying on them, and cite what you checked.
8. No new docs beyond ADR entries, CLAUDE.md updates, and code.
## Decisions locked (from the Step 0 audit; do not reopen without the human)
 
- App shape: Next.js BFF, same origin. The browser talks only to Next.js; Next.js server code talks to FastAPI. Do NOT add CORS middleware to the backend (it is deliberately absent; secure by default).
- Auth: backend stays OAuth2 bearer, header only. The BFF holds the JWT in an httpOnly cookie and attaches `Authorization: Bearer` on every proxied call. The browser never sees the token.
- Backend API surface (verify against code before use; prefix as found, e.g. /api/v1): auth register and login, users/me, documents (POST, list, detail, DELETE), conversations (POST, list, messages, POST message as SSE), usage (admin only, `days` 1..90), health (unauthenticated, deployment-wide).
- Honest backend change B1 is done now (spec below). The Usage card 'Where the time goes' is replaced by a real 'Average latency' card using the existing per-day `avg_latency_ms`.
- Omitted for now because the backend does not provide them: Documents 'Pages' column, stage-by-stage processing progress (use an indeterminate bar plus the status chip), 'Open in document' link, sidebar and Usage budget meter (needs a new endpoint and non-zero budget config). Each goes in the backlog list in CLAUDE.md with its proposed backend design (file access via short-lived signed URL, org-scoped, 404 for other orgs; budget endpoint returning used, limit, enabled).
- The /health status chip shows only the overall status word. Never render the names in `degraded[]` to users.
- Cap list sizes in the UI; no pagination work in this phase.
- Dark theme only (see documind-ui-design).
## B1: honest backend data for the UI (do first, tests first)
 
Goal: make the trace chips, source cards, and refusal states real, with the smallest change.
 
1. Citations SSE event gains: `confidence` (the gate score, only when the reranker is calibrated, otherwise null), `scores_calibrated` (bool), `used_reranker_fallback` (bool), `candidate_count`, `parent_count`, and per-stage timings `retrieve_ms`, `rerank_ms`, `first_token_ms` measured with a monotonic clock around retrieve, rerank, and first streamed token. Keep existing fields (`answer`, `citations`, `cached`, `gated`, `rewritten_question`).
2. Each citation payload gains `score` (from best_score, only when scores are calibrated, otherwise null) and `passage` (the parent passage text for CITED sources only, capped to a safe length such as 1200 characters, truncated at a sentence boundary; plus `matched_chunk_id`). The existing `snippet` stays.
3. Cached answers: do not invent timings. Replays return `cached: true` and null timings.
4. Scores that are rank-reciprocal (RRF, passthrough, reranker fallback) must NEVER be exposed as relevance. Gate on `scores_calibrated` and not on fallback.
5. Tests: payload shape; null score and null confidence under fallback and passthrough; timings present on live answers and null on cache hits; passage only for cited markers; passage is from the caller's own org (add an isolation test: org B cannot get org A passages through any path); no change to existing responses' meaning.
6. Mutation checks: expose score under fallback (a test must fail); include passages for uncited sources (a test must fail); drop the org filter from passage fetch (isolation test must fail). Revert after each.
7. Update the OpenAPI examples and the golden-set harness only if they parse this event; do not change eval results.
Stop at B1 and report.
## Architecture: the BFF
 
- `POST /api/auth/login` (route handler): calls backend login, sets cookie `dm_session`: HttpOnly, Secure in production, SameSite=Lax, Path=/, Max-Age matching token lifetime. Response body is the user profile only, never the token. Register behaves the same.
- `POST /api/auth/logout`: clears the cookie. The client clears the whole query cache.
- `app/api/[...path]/route.ts`: proxy to `BACKEND_URL` (server-only env var). Allowlist of backend path prefixes (auth, users/me, documents, conversations, usage, health); anything else returns 404. Attach the bearer from the cookie; never forward the browser cookie to the backend. Forward method, query, body; strip hop-by-hop headers; pass status codes and Retry-After through. On backend 401: clear the cookie and return 401 so the client redirects to login.
- SSE: return the upstream body as a stream with `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`; propagate client abort to the upstream request.
- CSRF: SameSite=Lax plus a required custom header `X-DocuMind-CSRF: 1` on every mutating request and an Origin check against the site. Reject otherwise.
- `middleware.ts` redirects to /login when the cookie is absent. This is UX only; the backend 401 is the real check.
- Security headers: CSP (default-src 'self', connect-src 'self', img-src 'self' data:, frame-ancestors 'none', nonce-based scripts; verify no violations), X-Content-Type-Options nosniff, Referrer-Policy strict-origin-when-cross-origin, minimal Permissions-Policy.
- Uploads: Vercel function request bodies are limited (verify current limit in the docs; target 4 MB). Cap in both the UI and the backend with a clear message. ADR: known limitation; production alternative is a presigned direct upload to object storage.
- Streaming spike first (in U1): a throwaway route proving SSE streams progressively through the BFF locally, and on a Vercel preview if the human has deployed one. Report honestly, including Vercel function duration limits.
## Data honesty rules for the UI
 
- Trace chips shown only when the field exists on the event: 'from cache' (cached), 'rewritten' (rewritten_question differs), 'retrieve N ms', 'rerank N ms', 'first token N ms', 'candidates N'. Confidence and relevance bars only when `scores_calibrated` is true.
- If any value is null, hide that element; never show 0 or a placeholder number.
- Usage cards map to real fields: Questions = requests; Tokens = llm_input_tokens + llm_output_tokens; Cache hit rate = cache_hits / requests; Refusal rate = refused / requests; stacked bar = cache_hits vs (ok minus cache_hits); Average latency from `avg_latency_ms`. Note that `cache_hit` is nullable, so pre-metering rows read as live; say so in a small footnote if old data is visible. Delta chips need a second request of twice the window, split client-side; hide them when days=1 or no prior period.
- Refusal state uses the message `kind` field; Stop uses client abort (the backend persists and caches nothing on cancel).
## Security rules for the UI
 
- The JWT is never readable by JavaScript: not in localStorage, sessionStorage, memory, URLs, or logs.
- LLM output and document text are untrusted. Render markdown with rehype-sanitize. No dangerouslySetInnerHTML with model or document text. Disable markdown images entirely (an image URL can exfiltrate data via a prompt-injected answer). Links: only http and https, rel='noopener noreferrer', show the domain.
- TanStack Query keys include user id and org id. Clear the cache on logout and on any 401.
- User-facing errors never include stack traces, backend URLs, or internal ids.
- No analytics or third-party scripts. Pin dependency versions; run `npm audit` and report high and critical.
## Tests
 
Playwright end-to-end against the real backend with fake providers (reuse the backend's offline fake configuration), plus network mocking for edge cases (429, 503, mid-stream error). Required:
1. Login success and failure; failure text is generic.
2. Cookie is HttpOnly (check via browser context cookies); `document.cookie` lacks it; storage holds no token.
3. Unauthenticated visit to a protected page redirects to login.
4. Member sees no upload, delete, or usage UI; direct URL to usage shows forbidden (backend also returns 403).
5. Upload leads to pending, processing, ready; failed shows the reason; oversize file rejected with a clear message.
6. Chat streams progressively; citation click opens the source panel with the right document, page, and passage.
7. Refusal state, error with retry, 429 with countdown from Retry-After, 503 budget state, Stop behaviour.
8. XSS: a mocked answer with `<img src=x onerror=...>`, a script tag, a markdown image, and a javascript: link renders inert.
9. Tenant isolation: logged in as org B, opening an org A document or chunk id shows not-found, never content.
10. A mutating BFF request without the CSRF header, or with a foreign Origin, is rejected.
11. Logout clears cookie and query cache; back button shows no cached org data.
12. Score and confidence elements are absent when `scores_calibrated` is false.
13. axe scan on every screen: zero serious or critical violations.
Mutation checks (apply, confirm a named test fails, revert, report a table): U1 remove HttpOnly; U2 disable sanitization; U3 enable markdown images; U4 forward the browser cookie to the backend; U5 remove the proxy allowlist; U6 remove the CSRF check; U7 stop clearing the query cache on logout; U8 render Usage for members; U9 render the score bar when scores are uncalibrated.
 
## Subagents (use if the files exist in .claude/agents/, else do the work inline and say so)
 
- `ui-builder`: implements one screen or component group; edits only the frontend directory.
- `ui-fidelity-reviewer`: read-only; judges screenshots and code against documind-ui-design.
- `ui-qa-runner`: runs Playwright and axe, saves screenshots, returns a compact table; never edits source.
- `ui-reviewer`: read-only security, tenant-safety, and state-coverage review.
Run ui-reviewer and ui-fidelity-reviewer at U4 and U6. Never let a subagent that can edit code also judge it.
## Checkpoints and order
 
- B1: backend honesty change (above). Stop.
- U1: Next.js scaffold, tokens, fonts, base components, dev-only `/design` page (documind-ui-design D0), plus BFF (login, logout, proxy, CSRF, middleware, headers) and the streaming spike. Stop.
- U2: app shell and Login/Register (design D1). Stop.
- U3: Documents with upload and polling (D2). Stop.
- U4: Chat with streaming, source panel, all states; run reviewers (D3). Stop.
- U5: Usage and the polish pass (D4, D5). Stop.
- U6: full test run, mutation table, npm audit, reviewer passes, ADRs (BFF and cookie, upload size limit, fetch-stream instead of EventSource, markdown sanitization, honest-data rules), CLAUDE.md Current Phase update and backlog list. Stop.
Report format: (1) what was built, 5 lines max; (2) Engineer note per feature; (3) tests and checks as a table with pass/fail and the command used; (4) NOT verified list; (5) decisions needed from the human, each with a recommendation and the tradeoff.
 
## Definition of done
 
B1 shipped with tests; all four screens work against the real backend; all required tests pass; all mutation checks fail as expected; axe has no serious or critical issues; npm audit has no unaddressed high or critical findings; no secret in the frontend bundle (grep the build output for key prefixes); no element shows data the backend did not produce; ADRs written; CLAUDE.md updated; the report lists everything not verified.