# ADR 0002 — A nonce-based CSP, and the dynamic rendering it forces

Status: accepted (U1, 2026-11-01)

## Context

Answers rendered in this UI are **model output over user-uploaded
document text** — the least trustworthy string in the system. A CSP is
the layer that still holds if markdown sanitisation is ever bypassed, so
it has to be a real one: no `'unsafe-inline'` in `script-src`.

Next.js offers three ways to get there:

1. A static CSP with `'unsafe-inline'` — rejected, because that is the
   directive an injected `<script>` needs.
2. Per-request nonces.
3. Experimental hash-based CSP via Subresource Integrity
   (`experimental.sri`), which keeps static rendering.

## Decision

Per-request nonces, generated in `proxy.ts`.

Next reads the `Content-Security-Policy` header off the incoming request,
extracts `'nonce-…'`, and applies it to its own framework and page
scripts automatically. `'strict-dynamic'` lets a nonced bundle load its
own chunks.

`export const dynamic = "force-dynamic"` in the root layout.

SRI was not chosen because it is explicitly experimental, and the thing
it buys — static generation and CDN caching — is worth nothing here (see
Consequences).

## Consequences

- **Static optimization, ISR, CDN caching of HTML, and Partial
  Prerendering are all off.** Next applies the nonce during server-side
  rendering, and a statically prerendered page was built when no request
  existed, so it ships script tags with no nonce that the CSP then
  blocks. The failure mode is a blank screen in production with nothing
  wrong in development, which is why this is a deliberate setting and not
  something to discover later.
- **The cost is near zero for this app.** Every screen behind `/login` is
  per-user and per-tenant and could never have been cached anyway.
- Development needs `'unsafe-eval'` in `script-src` (React uses `eval` to
  rebuild server stack traces) and `'unsafe-inline'` in `style-src` (the
  dev server injects un-nonced style tags for hot reloading). Both are
  gated on `NODE_ENV === "development"` and absent from production.
- `connect-src 'self'` is the canary for this whole architecture: if a
  backend host ever appears in that directive, the BFF has been bypassed.
- Fonts are self-hosted by `next/font`, so `font-src 'self'` needs no
  external host and first paint makes no third-party request.

## Note on the file name

This lives in **`proxy.ts`**, not `middleware.ts`. Next 16.0.0 deprecated
the `middleware` convention and renamed it to `proxy`, with the exported
function renamed to match. The phase skill says "middleware.ts"; that
wording predates Next 16. There is a codemod
(`npx @next/codemod middleware-to-proxy`) for existing projects.

## Verified

- `Content-Security-Policy` present on page responses, with a **different
  nonce on each of two consecutive requests**.
- Every `<script>` tag in the served HTML carries that request's nonce
  (21 nonce attributes against 19 script tags, Next having also nonced
  its preloads).
- `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` and
  `X-Frame-Options` present; `X-Powered-By` absent.
- After `force-dynamic`, the build reports every route as `ƒ (Dynamic)`.
  Before it, `/`, `/chat`, `/design` and `/login` were `○ (Static)` — the
  exact condition that would have shipped un-nonced scripts.
