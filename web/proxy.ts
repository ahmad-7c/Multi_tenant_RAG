import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { SESSION_COOKIE } from "@/lib/backend";

/**
 * Next 16 renamed the `middleware` file convention to `proxy`
 * (v16.0.0; `middleware.ts` is deprecated). Same role: runs before a
 * route renders. Defaults to the Node.js runtime in 16.
 *
 * Two jobs:
 *
 * 1. Per-request CSP nonce. Next extracts the nonce from the
 *    Content-Security-Policy header it receives and applies it to its
 *    own framework and page scripts, so there is no 'unsafe-inline'
 *    anywhere in production.
 * 2. Redirect to /login when the session cookie is absent. This is UX
 *    ONLY -- it saves a flash of an empty dashboard. The real check is
 *    the backend's 401, because a cookie's presence says nothing about
 *    whether its token is valid, expired, or for a deleted user.
 */

/**
 * Reachable without a session.
 *
 * `/design` is here because it is a dev-only component reference that
 * touches no API and no tenant data -- and it 404s in production, so
 * this exemption cannot expose anything there.
 */
const AUTH_EXEMPT = [
  // The marketing site. "/" matches the root EXACTLY here, not every
  // path -- `matches` compares equality or a "/"-delimited prefix, and
  // no real path starts with "//".
  "/",
  "/how-it-works",
  "/how-its-built",
  "/login",
  "/register",
  "/design",
];

/**
 * The sign-in screens specifically. Separate from AUTH_EXEMPT because
 * an authenticated user should be bounced off these to the app, but NOT
 * off /design -- bouncing a developer away from the component reference
 * the moment they log in would be a daft way to lose an afternoon.
 */
const AUTH_LANDING = ["/login", "/register"];

const matches = (pathname: string, paths: string[]) =>
  paths.some((p) => pathname === p || pathname.startsWith(`${p}/`));

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const isDev = process.env.NODE_ENV === "development";

  const csp = [
    "default-src 'self'",
    // strict-dynamic lets a nonced script load its own chunks, which is
    // how the App Router loads route bundles. 'unsafe-eval' is dev-only:
    // React uses eval to rebuild server stack traces in the browser.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    // Dev needs unsafe-inline for styles because the dev server injects
    // un-nonced style tags for hot reloading.
    `style-src 'self' ${isDev ? "'unsafe-inline'" : `'nonce-${nonce}'`}`,
    // data: is needed for the self-hosted font files next/font inlines.
    "img-src 'self' data:",
    "font-src 'self'",
    // 'self' only: the browser talks to this origin and nothing else.
    // The backend is reachable only through our own route handlers, so
    // no backend host belongs here -- if one ever appears in this
    // directive, the BFF has been bypassed.
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(isDev ? [] : ["upgrade-insecure-requests"]),
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const { pathname } = request.nextUrl;
  const hasSession = request.cookies.has(SESSION_COOKIE);

  if (!hasSession && !matches(pathname, AUTH_EXEMPT)) {
    const target = new URL("/login", request.url);
    // Carried so the user lands where they were going. Only a PATH is
    // kept, never an absolute URL from the request: echoing one back
    // into a redirect is an open-redirect gadget.
    if (pathname !== "/") target.searchParams.set("next", pathname);
    return NextResponse.redirect(target);
  }

  if (hasSession && matches(pathname, AUTH_LANDING)) {
    return NextResponse.redirect(new URL("/chat", request.url));
  }

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // API routes are excluded: they are JSON and SSE, not documents,
      // so a CSP does nothing for them, and the proxy handler does its
      // own auth. Static assets are excluded so a redirect can never
      // swallow the CSS.
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      // Prefetches do not need a nonce and should never trigger a
      // redirect -- a prefetch of a protected page while logged out
      // would otherwise burn a redirect on a navigation nobody made.
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
