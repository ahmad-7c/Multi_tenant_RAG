/**
 * The CSRF header name and value, shared by the browser client and the
 * server-side check.
 *
 * Split out of csrf.ts because that module is `server-only` -- the
 * check itself must never ship to the browser, but the client has to
 * know which header to send. Neither value is a secret: the defence is
 * that a cross-origin caller CANNOT SET a custom header without a CORS
 * preflight, which this app never answers. A shared constant is
 * therefore safe to bundle, and keeping it in one place is what stops
 * the two sides disagreeing about the spelling.
 */
export const CSRF_HEADER = "x-documind-csrf";
export const CSRF_VALUE = "1";
