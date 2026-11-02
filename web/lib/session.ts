import "server-only";

import { cookies } from "next/headers";

import { SESSION_COOKIE, SESSION_MAX_AGE_SECONDS } from "./backend";

/**
 * The backend JWT, or null.
 *
 * Read only on the server. There is no client-side counterpart on
 * purpose: the cookie is httpOnly, so `document.cookie` cannot see it
 * and no amount of XSS can read it out. A "convenience" helper that
 * copied it into memory or localStorage would undo that.
 */
export async function readToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

export async function setSession(token: string): Promise<void> {
  const store = await cookies();
  store.set(SESSION_COOKIE, token, {
    httpOnly: true,
    // Off in development because dev runs on plain http://localhost and
    // a Secure cookie would simply never be stored -- which looks like
    // a broken login rather than a cookie policy.
    secure: process.env.NODE_ENV === "production",
    // Lax, not Strict: Strict drops the cookie on any cross-site
    // navigation INCLUDING a plain link into the app, so a user
    // following a link from email would land logged out. Lax still
    // withholds it from cross-site POSTs, which is the CSRF case, and
    // the custom-header + Origin check in csrf.ts covers the rest.
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
}

export async function clearSession(): Promise<void> {
  const store = await cookies();
  // Delete AND overwrite with an expired empty value. `delete` alone
  // relies on the browser honouring a removal for the exact
  // name/path/domain triple; an expired set is the belt to that brace.
  store.set(SESSION_COOKIE, "", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  store.delete(SESSION_COOKIE);
}
