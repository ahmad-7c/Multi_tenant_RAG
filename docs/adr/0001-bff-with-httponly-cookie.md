# ADR 0001 — A Next.js BFF holds the JWT in an httpOnly cookie

Status: accepted (U1, 2026-11-01)

## Context

The backend authenticates with `OAuth2PasswordBearer` — a JWT in an
`Authorization` header — and has **no CORS middleware at all**. The Step 0
audit confirmed that is deliberate, not an omission: `app/main.py` has no
`add_middleware` call.

A browser SPA talking straight to FastAPI would need two things we do not
want:

1. CORS on the backend, which turns "any origin a misconfiguration
   allows" into a reachable API.
2. The JWT in the browser — localStorage, sessionStorage, or JS memory.
   All three are readable by any script that executes on the page, so one
   XSS becomes a stolen token valid for its full 24-hour lifetime.

## Decision

The browser talks only to Next.js. Next.js server code talks to FastAPI.

- `POST /api/auth/login` and `/api/auth/register` exchange credentials for
  a token, set it as an **httpOnly** cookie (`dm_session`), and return the
  user **profile** in the body.
- `app/api/[...path]/route.ts` proxies the allowlisted backend paths,
  reading the cookie server-side and minting `Authorization: Bearer`.
- **No CORS is added to the backend.** There is no cross-origin request
  left to permit.

## Consequences

- `document.cookie` cannot see the token, so XSS cannot exfiltrate it.
  This is the main thing bought.
- Login costs **two** upstream calls, not one: `/auth/login` returns only
  `{access_token}`, so the profile needs a follow-up `/users/me`. Register
  costs three (register, login, me) because the backend's register returns
  no token. Accepted — a client that has to call login itself is a client
  holding a password for a second round trip.
- `auth` is deliberately **absent** from the proxy allowlist. A generic
  passthrough of `/auth/login` would return `{access_token}` straight to
  the browser and undo this entire ADR in one word.
- The backend is now unreachable from the browser except through the
  allowlist, so every new backend endpoint is invisible to the UI until
  someone adds it on purpose.
- Logout clears the cookie but **cannot revoke the token**: the backend
  issues stateless JWTs with no server-side session. The token stays valid
  until its `exp`. What logout guarantees is that this browser no longer
  holds it.
- Server-side rendering cannot be cached per-user; see ADR 0002.

## Verified

Through the running BFF against the real backend (U1):

- Register returned 201 with a profile body and **no** `access_token` and
  no JWT anywhere in the body.
- `Set-Cookie: dm_session=…; Path=/; Max-Age=86400; HttpOnly; SameSite=lax`
  (`Secure` absent in dev only, because dev is plain http).
- `/api/users/me`, `/api/documents`, `/api/usage`, `/api/conversations` all
  200 with **only** the cookie present; 401 without it.
- Logout set an expired empty cookie and the next `/api/users/me` was 401.
- `GET /api/auth/login` returns 405, proving the dedicated handler wins
  over the catch-all rather than the catch-all proxying it.
