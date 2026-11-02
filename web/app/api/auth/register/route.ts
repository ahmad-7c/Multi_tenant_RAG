import { NextResponse } from "next/server";

import { BACKEND_URL } from "@/lib/backend";
import { authenticate } from "@/lib/auth-flow";
import { checkCsrf } from "@/lib/csrf";
import { setSession } from "@/lib/session";

/**
 * Create an organization plus its first admin, then sign them in.
 *
 * The backend's /auth/register returns the new ids and NO token, so a
 * second call is needed to end up in the same state as login: cookie
 * set, profile returned. Done here rather than making the client POST
 * twice, because a client that has to call login itself is a client
 * holding a password for a second round trip.
 */
export async function POST(request: Request) {
  const csrf = checkCsrf(request);
  if (csrf) {
    console.warn(`[bff] rejected register: ${csrf.reason}`);
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

  let created: Response;
  try {
    created = await fetch(`${BACKEND_URL}/api/v1/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ org_name, email, password }),
      cache: "no-store",
    });
  } catch (error) {
    console.error("[bff] register upstream failed", error);
    return NextResponse.json(
      { error: "The service is unavailable. Try again shortly." },
      { status: 502 },
    );
  }

  if (!created.ok) {
    // 409 is the one failure worth naming: the org name is taken, which
    // the user can act on and which reveals nothing about a credential.
    // 422 is relayed as a generic validation message because its body
    // is a Pydantic error tree, not prose for a person.
    const message =
      created.status === 409
        ? "That organization name is already taken."
        : created.status === 422
          ? "Check the organization name, email and password."
          : created.status === 429
            ? "Too many attempts. Wait a moment and try again."
            : "The service is unavailable. Try again shortly.";
    const status = [409, 422, 429].includes(created.status)
      ? created.status
      : 502;
    if (status === 502) {
      console.error(`[bff] register returned ${created.status}`);
    }
    return NextResponse.json({ error: message }, { status });
  }

  const result = await authenticate({ org_name, email, password });
  if (!result.ok) {
    // The account EXISTS at this point. Saying so matters: telling the
    // user to try again would have them hit "name already taken".
    console.error("[bff] register succeeded but the follow-up login failed");
    return NextResponse.json(
      { error: "The organization was created. Sign in to continue." },
      { status: 502 },
    );
  }

  await setSession(result.token);
  return NextResponse.json({ user: result.profile }, { status: 201 });
}
