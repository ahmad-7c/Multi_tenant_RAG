import "server-only";

import { CSRF_HEADER, CSRF_VALUE } from "./csrf-shared";

export { CSRF_HEADER, CSRF_VALUE };

/**
 * CSRF defence for a cookie-authenticated BFF.
 *
 * SameSite=Lax already withholds the session cookie from cross-site
 * POSTs, which covers the classic form-submission attack. These two
 * checks exist because SameSite is a browser-enforced policy and not
 * every client enforces it identically (older browsers, embedded
 * webviews), and because Lax does NOT cover a same-site-but-different-
 * subdomain attacker.
 *
 * Two independent checks:
 *
 * 1. A required custom header. A cross-origin <form> POST cannot set
 *    one, and setting it from JS makes the request a preflighted CORS
 *    request -- which this app answers with no CORS headers at all, so
 *    the browser refuses it. That is why the header's VALUE does not
 *    matter and is not a secret: its presence is the signal.
 * 2. An Origin check against the request's own host. Covers the case
 *    where a client sends the header anyway.
 */
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export type CsrfFailure = { reason: string } | null;

/**
 * Returns null when the request may proceed, or a reason to reject.
 *
 * The reason is for the SERVER LOG. The response body says only that
 * the request was rejected: telling an attacker which of the two
 * checks they failed is free reconnaissance.
 */
export function checkCsrf(request: Request): CsrfFailure {
  if (SAFE_METHODS.has(request.method)) return null;

  if (request.headers.get(CSRF_HEADER) !== CSRF_VALUE) {
    return { reason: "missing or wrong CSRF header" };
  }

  const origin = request.headers.get("origin");
  if (origin === null) {
    // A same-origin fetch from our own app always sends Origin on a
    // mutating request, so an absent one is not something the UI
    // produces. Rejected rather than waved through: "no Origin" is
    // exactly what a non-browser client sends.
    return { reason: "no Origin header on a mutating request" };
  }

  let originHost: string;
  try {
    originHost = new URL(origin).host;
  } catch {
    return { reason: `unparseable Origin: ${origin}` };
  }

  // Compared against the request's own Host rather than a configured
  // site URL, so this keeps working on every Vercel preview
  // deployment without a per-environment variable to forget.
  const host = request.headers.get("host");
  if (!host || originHost !== host) {
    return { reason: `Origin ${originHost} does not match host ${host}` };
  }

  return null;
}
