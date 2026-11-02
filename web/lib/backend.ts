// Server-only. `server-only` makes an accidental client import a BUILD
// error rather than a bundle that ships BACKEND_URL to the browser.
import "server-only";

/**
 * Where FastAPI lives. Server-side only -- deliberately NOT
 * NEXT_PUBLIC_*, because the browser must never learn the backend's
 * address. The whole point of the BFF is that the only origin the
 * browser knows is this one.
 */
export const BACKEND_URL = (
  // The fallback matches .env.local.example so one number appears
  // everywhere. 8010 because host 8000 is taken on the dev box.
  process.env.BACKEND_URL ?? "http://127.0.0.1:8010"
).replace(/\/+$/, "");

/** The httpOnly session cookie. Holds the backend JWT. */
export const SESSION_COOKIE = "dm_session";

/**
 * Cookie lifetime. Matches the backend's JWT_EXPIRE_MINUTES (default
 * 60 * 24). A cookie that outlives its token leaves the user
 * "logged in" with a 401 behind every request; a cookie that dies
 * first logs them out while the token is still good.
 */
export const SESSION_MAX_AGE_SECONDS = Number(
  process.env.SESSION_MAX_AGE_SECONDS ?? 60 * 60 * 24,
);

/**
 * Vercel caps a function's request AND response body at 4.5 MB and
 * returns 413 FUNCTION_PAYLOAD_TOO_LARGE above it (verified against
 * vercel.com/docs/functions/limitations, 2026-08-24). 4 MB leaves
 * headroom for multipart overhead.
 *
 * NB the backend's own MAX_UPLOAD_BYTES is 25 MB, which is correct for
 * a self-hosted deployment. This is the smaller, platform-imposed
 * limit, enforced here so the user gets a sentence instead of a
 * platform error page. See ADR 0003.
 */
export const MAX_UPLOAD_BYTES = 4 * 1024 * 1024;

// The pure rules live in their own module so they can be imported
// by a test; re-exported here so every call site is unchanged.
export { forwardableHeaders, resolveBackendPath } from "./proxy-rules";
export type { Resolved } from "./proxy-rules";
