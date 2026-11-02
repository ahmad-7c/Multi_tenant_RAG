import "server-only";

import { redirect } from "next/navigation";

import { BACKEND_URL } from "./backend";
import { readToken } from "./session";
import type { User } from "./types";

/**
 * Server-side reads, for layouts and pages that render before any
 * client code runs.
 *
 * These call FastAPI DIRECTLY rather than going through our own
 * /api/* proxy. Going through the proxy from a server component would
 * mean the server making an HTTP request to itself -- an extra hop,
 * and one that needs an absolute URL the server has to guess. The
 * proxy exists for the BROWSER; the server already has the cookie and
 * the backend address.
 */

export async function getCurrentUser(): Promise<User | null> {
  const token = await readToken();
  if (!token) return null;

  let response: Response;
  try {
    response = await fetch(`${BACKEND_URL}/api/v1/users/me`, {
      headers: { authorization: `Bearer ${token}` },
      // Never cached: this is per-user and the cache is shared.
      cache: "no-store",
    });
  } catch (error) {
    console.error("[app] could not reach the API for /users/me", error);
    return null;
  }

  if (!response.ok) return null;
  return (await response.json()) as User;
}

/**
 * The current user, or a redirect to /login.
 *
 * This is the REAL authentication check for a page. proxy.ts also
 * redirects when the cookie is absent, but a cookie's presence says
 * nothing about whether its token is valid, expired, or belongs to a
 * deleted user -- only the backend can answer that.
 */
export async function requireUser(): Promise<User> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}

/**
 * The deployment's health, for the status chip.
 *
 * Returns the STATUS WORD only. `degraded[]` names internal
 * subsystems ("reranker_disabled", "embedding_model_mismatch") and is
 * operator-level deployment detail -- it is deliberately dropped here
 * rather than filtered in the component, so there is no component
 * holding it that a later edit could render.
 */
export async function getHealthStatus(): Promise<string | null> {
  try {
    const response = await fetch(`${BACKEND_URL}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2500),
    });
    if (!response.ok) return null;
    const body = (await response.json()) as { status?: string };
    return body.status ?? null;
  } catch {
    // A health check that cannot complete is not an app error -- the
    // chip just shows unknown.
    return null;
  }
}
