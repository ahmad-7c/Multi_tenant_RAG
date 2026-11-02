import { NextResponse } from "next/server";

import { checkCsrf } from "@/lib/csrf";
import { clearSession } from "@/lib/session";

/**
 * Clear the session cookie.
 *
 * POST, not GET: a GET logout can be triggered by any <img src> on any
 * page on the internet. The client clears its whole query cache after
 * this resolves -- the cookie going away does not evict one org's
 * documents from an in-memory cache the next user of this browser
 * could still read.
 *
 * There is nothing to revoke upstream: the backend issues stateless
 * JWTs with no server-side session to end. The token stays valid until
 * its exp; what logout guarantees is that this browser no longer holds
 * it. Worth knowing rather than assuming otherwise.
 */
export async function POST(request: Request) {
  const csrf = checkCsrf(request);
  if (csrf) {
    console.warn(`[bff] rejected logout: ${csrf.reason}`);
    return NextResponse.json({ error: "Request rejected." }, { status: 403 });
  }

  await clearSession();
  return NextResponse.json({ ok: true });
}
