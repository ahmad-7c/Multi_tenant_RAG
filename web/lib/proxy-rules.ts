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

/**
 * The client-IP pair. Deliberately NOT `x-forwarded-for`: Caddy
 * overwrites that one with the connection's own peer address
 * (`header_up X-Forwarded-For {remote_host}`), so anything sent in it
 * is discarded at the edge. A distinct name passes through untouched,
 * which is what keeps this change out of the Caddyfile.
 */
export const CLIENT_IP_HEADER = "x-client-ip";
export const CLIENT_IP_SECRET_HEADER = "x-client-ip-secret";

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
  // The client-IP pair, STRIPPED ON THE WAY IN. The backend trusts
  // these when the secret matches, so a browser that could set them
  // would choose its own rate-limit bucket -- a fresh empty one per
  // request, which is strictly worse than no per-IP limit at all
  // because the limit would still look like it was working.
  //
  // Stripping happens here, before anything sets them, so the forged
  // value is gone whether or not the BFF goes on to add a real one.
  // The order is the whole defence: strip, then set.
  CLIENT_IP_HEADER,
  CLIENT_IP_SECRET_HEADER,
]);

export function forwardableHeaders(incoming: Headers): Headers {
  const out = new Headers();
  incoming.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out.set(key, value);
  });
  return out;
}

/**
 * The browser's real IP, as attested by the platform.
 *
 * `x-vercel-forwarded-for` is preferred, and the preference is not
 * arbitrary. Vercel's request-headers reference says of
 * `x-forwarded-for`: "we currently overwrite the X-Forwarded-For header
 * and do not forward external IPs. This restriction is in place to
 * prevent IP spoofing." So a caller-supplied value is discarded --
 * EXCEPT that custom X-Forwarded-For is purchasable as a Trusted Proxy
 * on Enterprise plans, and the docs note of `x-vercel-forwarded-for`
 * that it is "identical to the x-forwarded-for header. However,
 * x-forwarded-for could be overwritten if you're using a proxy on top
 * of Vercel."
 *
 * `x-real-ip` is documented as "identical to the x-forwarded-for
 * header", so it carries the same caveat and is only a fallback.
 *
 * Net: on this plan none of the three can be spoofed by the caller, and
 * `x-vercel-forwarded-for` is the one that stays correct if a CDN is
 * ever put in front. Verified against
 * vercel.com/docs/headers/request-headers (last updated 2025-12-13).
 *
 * Returns the LEFTMOST entry: the list is client, then proxies.
 */
export function platformClientIp(incoming: Headers): string | null {
  for (const header of [
    "x-vercel-forwarded-for",
    "x-real-ip",
    "x-forwarded-for",
  ]) {
    const raw = incoming.get(header);
    if (!raw) continue;
    const first = raw.split(",")[0].trim();
    // Capped here as well as in the backend. The value becomes part of
    // a Redis key, and an unbounded one is a memory problem wearing a
    // header's clothing.
    if (first && first.length <= 64) return first;
  }
  return null;
}
