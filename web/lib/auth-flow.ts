import "server-only";

import { BACKEND_URL } from "./backend";

/**
 * The shared half of login and register.
 *
 * Both end the same way: exchange credentials for a token, keep the
 * token server-side, and return the PROFILE to the browser. The
 * backend's /auth/login returns `{access_token}` and nothing else, so
 * the profile needs a second call to /users/me. Two round trips on a
 * login is a fair price for the browser never seeing a credential.
 */

export type Profile = {
  id: string;
  org_id: string;
  email: string;
  role: "admin" | "member";
};

export type AuthResult =
  | { ok: true; token: string; profile: Profile }
  | { ok: false; status: number; message: string };

/**
 * One generic message for every credential failure.
 *
 * Never "no such organization" or "wrong password": each of those
 * confirms half the credential to someone guessing, which turns a
 * login form into an account-enumeration oracle. The backend's
 * per-IP rate limit on login is the other half of this defence.
 */
const GENERIC = "Those details did not match an account.";

export async function authenticate(credentials: {
  org_name: string;
  email: string;
  password: string;
}): Promise<AuthResult> {
  let tokenResponse: Response;
  try {
    tokenResponse = await fetch(`${BACKEND_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(credentials),
      cache: "no-store",
    });
  } catch (error) {
    console.error("[bff] login upstream failed", error);
    return {
      ok: false,
      status: 502,
      message: "The service is unavailable. Try again shortly.",
    };
  }

  if (tokenResponse.status === 429) {
    // Passed through as-is: a wait is actionable, unlike the generic
    // message, and the backend already rounds Retry-After up.
    return {
      ok: false,
      status: 429,
      message: "Too many attempts. Wait a moment and try again.",
    };
  }
  if (!tokenResponse.ok) {
    return { ok: false, status: 401, message: GENERIC };
  }

  const { access_token: token } = (await tokenResponse.json()) as {
    access_token?: string;
  };
  if (!token) {
    console.error("[bff] login succeeded with no access_token in the body");
    return { ok: false, status: 502, message: "The service is unavailable." };
  }

  const meResponse = await fetch(`${BACKEND_URL}/api/v1/users/me`, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!meResponse.ok) {
    console.error(`[bff] /users/me returned ${meResponse.status} after login`);
    return { ok: false, status: 502, message: "The service is unavailable." };
  }

  return { ok: true, token, profile: (await meResponse.json()) as Profile };
}
