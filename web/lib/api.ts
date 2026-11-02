import { CSRF_HEADER, CSRF_VALUE } from "./csrf-shared";

/**
 * The browser's only way to reach the API.
 *
 * Every request goes to this origin. There is no backend URL here and
 * no token: the session cookie rides along automatically because it is
 * same-origin, and the BFF turns it into a bearer header server-side.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, message: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds ?? null;
  }
}

/** Called on any 401 so the app can drop cached tenant data. */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

function parseRetryAfter(response: Response): number | undefined {
  const raw = response.headers.get("retry-after");
  if (!raw) return undefined;
  const seconds = Number(raw);
  if (Number.isFinite(seconds)) return Math.ceil(seconds);
  // RFC 9110 allows an HTTP-date as well as delay-seconds.
  const when = Date.parse(raw);
  if (Number.isNaN(when)) return undefined;
  return Math.max(0, Math.ceil((when - Date.now()) / 1000));
}


/**
 * A sentence for a status code, for when the body gives us nothing
 * usable.
 *
 * "Something went wrong" is true of every failure and useful for none:
 * a 413 resolves by picking a smaller file, a 429 by waiting, a 403 by
 * asking an administrator. Telling someone which is the difference
 * between a dead end and a next step.
 */
function genericMessageFor(status: number): string {
  if (status === 400 || status === 422) return "Some of those details were not valid.";
  if (status === 403) return "You do not have access to that.";
  if (status === 404) return "That could not be found.";
  if (status === 413) return "That file is too large.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500) return "The service is unavailable. Try again shortly.";
  return "Something went wrong.";
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  // Required on every mutating request by the BFF's CSRF check. Set
  // here, once, so no call site can forget it.
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set(CSRF_HEADER, CSRF_VALUE);
    if (init.body !== undefined && !headers.has("content-type")) {
      // FormData must set its own multipart boundary, so it is left
      // alone; anything else here is JSON.
      if (!(init.body instanceof FormData)) {
        headers.set("content-type", "application/json");
      }
    }
  }

  const response = await fetch(`/api/${path.replace(/^\/+/, "")}`, {
    ...init,
    method,
    headers,
    // Same-origin: the cookie is sent, and there is no cross-origin
    // request for the browser to consider.
    credentials: "same-origin",
  });

  // A 401 from an AUTH endpoint is not an expired session -- it is the
  // credentials being rejected, which is the whole point of that
  // request. Treating the two the same replaced "Those details did not
  // match an account" with "Your session has ended. Sign in again." on
  // the login form, which is both wrong and baffling: the user has no
  // session to have ended. Caught by e2e test 1b.
  const isAuthAttempt = path.replace(/^\/+/, "").startsWith("auth/");

  if (response.status === 401 && !isAuthAttempt) {
    onUnauthorized?.();
    throw new ApiError(401, "Your session has ended. Sign in again.");
  }

  if (!response.ok) {
    let message = genericMessageFor(response.status);
    try {
      const body = (await response.json()) as {
        error?: unknown;
        detail?: unknown;
      };
      // `error` from our own handlers, `detail` from FastAPI -- but
      // ONLY when it is a plain string. FastAPI returns an ARRAY of
      // Pydantic error objects for a 422, which rendered as
      // "[object Object]" in the UI, and an unexpected shape could
      // carry internal detail. Anything that is not a sentence falls
      // back to a message written for a person.
      const candidate = typeof body.error === "string" ? body.error : body.detail;
      if (typeof candidate === "string" && candidate.trim()) {
        message = candidate;
      }
    } catch {
      // A non-JSON error body (a platform error page). The status is
      // the only reliable signal.
    }
    throw new ApiError(response.status, message, parseRetryAfter(response));
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Query keys are namespaced by org AND user. */
export const queryKeys = {
  documents: (orgId: string, userId: string) =>
    ["documents", orgId, userId] as const,
  document: (orgId: string, userId: string, id: string) =>
    ["documents", orgId, userId, id] as const,
  conversations: (orgId: string, userId: string) =>
    ["conversations", orgId, userId] as const,
  messages: (orgId: string, userId: string, id: string) =>
    ["conversations", orgId, userId, id, "messages"] as const,
  usage: (orgId: string, userId: string, days: number) =>
    ["usage", orgId, userId, days] as const,
  health: () => ["health"] as const,
};
