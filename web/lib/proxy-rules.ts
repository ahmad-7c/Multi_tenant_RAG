/**
 * Pure proxy rules: which backend paths are reachable, and which
 * headers may be forwarded.
 *
 * Deliberately NOT `server-only`, unlike lib/backend.ts. These are
 * decisions expressed as functions -- they hold no URL, no secret
 * and read no environment -- and keeping them importable is what
 * lets them be tested directly. The server-only module imports and
 * re-exports them, so call sites are unchanged.
 *
 * The split exists because the alternative was leaving the
 * allowlist and the header-stripping list with no test at all:
 * neither has a visible surface, so neither is reachable from a
 * browser test.
 */

/**
 * The ONLY backend paths the browser can reach, as browser path ->
 * backend path.
 *
 * An allowlist, not a denylist: a new backend route is unreachable
 * until someone adds it here on purpose. The reverse -- proxying
 * everything and blocking known-bad paths -- means every future
 * backend endpoint is exposed the moment it is written, including
 * internal ones nobody meant to publish.
 *
 * `auth` is DELIBERATELY ABSENT. The backend's /auth/login returns
 * `{access_token}`; a generic passthrough would hand that JSON
 * straight to the browser and defeat the httpOnly cookie entirely.
 * Login, register and logout have their own route handlers that keep
 * the token server-side. Adding "auth" to this list would be a
 * one-word credential leak.
 */
const ALLOWED_PREFIXES = ["users", "documents", "conversations", "usage"];

export type Resolved = { backendPath: string } | null;

/**
 * Resolve a browser path to a backend path, or null to 404.
 *
 * Returning null (not 403) for an unknown path matches the backend's
 * own reasoning about cross-tenant ids: a 403 confirms something
 * exists.
 */
export function resolveBackendPath(segments: string[]): Resolved {
  if (segments.length === 0) return null;
  // No traversal, no empty segments. `..` in a path segment cannot
  // escape a URL join here, but rejecting it keeps the allowlist
  // honest about what it matched.
  if (segments.some((s) => s === "" || s === "." || s === "..")) return null;

  const path = segments.join("/");

  // /health sits at the backend root, not under /api/v1, and is
  // unauthenticated. Mapped explicitly rather than pattern-matched so
  // the exception is visible.
  if (path === "health") return { backendPath: "/health" };

  if (!ALLOWED_PREFIXES.includes(segments[0])) return null;
  return { backendPath: `/api/v1/${path}` };
}

/**
 * Headers that describe ONE hop and must not be forwarded. Copying
 * these upstream corrupts the next connection: a `content-length` from
 * a body we may not resend, a `connection: keep-alive` that means
 * something different on the new socket, an `accept-encoding` whose
 * response we would then pass through already-encoded.
 *
 * `cookie` is here for a different and more important reason: the
 * browser's cookie jar is for THIS origin. Forwarding it would send
 * the session cookie to FastAPI, which authenticates by `Authorization`
 * header -- so it would be ignored at best, and at worst logged
 * upstream. The BFF mints the Authorization header instead.
 */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
  "accept-encoding",
  "host",
  "cookie",
  // Never let a caller supply its own bearer: the cookie is the only
  // source of identity here.
  "authorization",
]);

export function forwardableHeaders(incoming: Headers): Headers {
  const out = new Headers();
  incoming.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out.set(key, value);
  });
  return out;
}
