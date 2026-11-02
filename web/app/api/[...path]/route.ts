import { NextResponse } from "next/server";

import {
  BACKEND_URL,
  forwardableHeaders,
  resolveBackendPath,
} from "@/lib/backend";
import { checkCsrf } from "@/lib/csrf";
import { clearSession, readToken } from "@/lib/session";

/**
 * The BFF proxy: the only path from the browser to FastAPI.
 *
 * The browser never holds the JWT. It sends an httpOnly cookie to this
 * origin; this handler reads it server-side and mints the
 * `Authorization: Bearer` header FastAPI expects. That is why the
 * backend needs no CORS middleware -- there is no cross-origin request
 * to permit.
 *
 * Anything not on the allowlist in lib/backend.ts is a 404 here, and
 * `/api/auth/*` never reaches this file: those are separate handlers,
 * because the backend's login response contains the token itself.
 */

// Streaming answers can outlive a default function timeout. 300s is the
// Hobby maximum and the Pro default on Vercel (verified against
// vercel.com/docs/functions/limitations, 2026-08-24); the backend's own
// LLM_TOTAL_TIMEOUT_SECONDS is 120s, so the budget that actually bites
// is the backend's, which is the right way round.
export const maxDuration = 300;

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(request: Request, ctx: Ctx): Promise<Response> {
  const csrf = checkCsrf(request);
  if (csrf) {
    // Logged with the reason, answered without it.
    console.warn(`[bff] rejected ${request.method}: ${csrf.reason}`);
    return NextResponse.json({ error: "Request rejected." }, { status: 403 });
  }

  const { path } = await ctx.params;
  const resolved = resolveBackendPath(path ?? []);
  if (!resolved) {
    return NextResponse.json({ error: "Not found." }, { status: 404 });
  }

  const token = await readToken();
  const incomingUrl = new URL(request.url);
  const target = `${BACKEND_URL}${resolved.backendPath}${incomingUrl.search}`;

  const headers = forwardableHeaders(request.headers);
  if (token) headers.set("authorization", `Bearer ${token}`);

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      // Streamed straight through rather than buffered, so an SSE
      // answer reaches the browser token by token instead of arriving
      // as one blob when the model finishes.
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : request.body,
      // Required by undici whenever body is a stream.
      // @ts-expect-error -- duplex is not in the lib DOM types yet.
      duplex: "half",
      // THE line that makes "Stop" work. When the browser aborts, this
      // signal aborts the upstream request, the backend sees the
      // disconnect, raises CancelledError into the provider coroutine
      // and stops paying for tokens nobody will read.
      signal: request.signal,
      redirect: "manual",
      cache: "no-store",
    });
  } catch (error) {
    if (request.signal.aborted) {
      // The user navigated away or pressed Stop. Not an error.
      return new Response(null, { status: 499 });
    }
    // The backend URL is deliberately absent from the body: a user-
    // facing error naming an internal host is reconnaissance.
    console.error("[bff] upstream fetch failed", error);
    return NextResponse.json(
      { error: "The service is unavailable. Try again shortly." },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers();
  for (const name of [
    "content-type",
    "retry-after",
    "cache-control",
    "content-disposition",
  ]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }

  const isEventStream = (upstream.headers.get("content-type") ?? "").includes(
    "text/event-stream",
  );
  if (isEventStream) {
    // no-transform stops a proxy from buffering or gzipping the stream
    // into uselessness; X-Accel-Buffering is the nginx-specific form of
    // the same instruction. Without them "streaming" can still arrive
    // as one delayed blob.
    responseHeaders.set("cache-control", "no-cache, no-transform");
    responseHeaders.set("x-accel-buffering", "no");
    responseHeaders.set("connection", "keep-alive");
  }

  if (upstream.status === 401) {
    // The token is dead or was never valid. Clear the cookie so the
    // next navigation goes to /login instead of looping through a
    // request that will 401 again.
    await clearSession();
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
