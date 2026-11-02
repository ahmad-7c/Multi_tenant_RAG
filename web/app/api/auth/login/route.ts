import { NextResponse } from "next/server";

import { authenticate } from "@/lib/auth-flow";
import { checkCsrf } from "@/lib/csrf";
import { setSession } from "@/lib/session";

/**
 * Exchange credentials for an httpOnly cookie.
 *
 * The response body is the user PROFILE. The token is set as a cookie
 * and never serialised into the body -- this handler exists instead of
 * proxying /auth/login precisely because that endpoint's own response
 * is `{access_token}`.
 */
export async function POST(request: Request) {
  const csrf = checkCsrf(request);
  if (csrf) {
    console.warn(`[bff] rejected login: ${csrf.reason}`);
    return NextResponse.json({ error: "Request rejected." }, { status: 403 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }

  const { org_name, email, password } = (body ?? {}) as Record<string, unknown>;
  if (
    typeof org_name !== "string" ||
    typeof email !== "string" ||
    typeof password !== "string"
  ) {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }

  const result = await authenticate({ org_name, email, password });
  if (!result.ok) {
    return NextResponse.json(
      { error: result.message },
      { status: result.status },
    );
  }

  await setSession(result.token);
  return NextResponse.json({ user: result.profile });
}
