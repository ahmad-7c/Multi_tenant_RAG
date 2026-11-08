import { expect, test } from "@playwright/test";

import {
  forwardableHeaders,
  platformClientIp,
  resolveBackendPath,
} from "../lib/proxy-rules";
import { registerAndLogin } from "./helpers";

/**
 * The two BFF guarantees that have no visible surface.
 *
 * Mutations U4 (forward the browser cookie upstream) and U5 (remove
 * the proxy allowlist) change nothing a user can see, so neither is
 * observable from a page. They are asserted directly against the
 * functions instead -- and the allowlist additionally end to end,
 * because a unit test proves the function is right while the e2e test
 * proves it is actually WIRED to the route.
 */

test.describe("header forwarding", () => {
  test("U4. the browser's cookie and authorization headers are never forwarded", () => {
    const incoming = new Headers({
      cookie: "dm_session=secret-token; other=1",
      authorization: "Bearer someone-elses-token",
      "content-type": "application/json",
      "x-documind-csrf": "1",
      host: "localhost:3020",
      "content-length": "42",
      "accept-encoding": "gzip",
    });

    const forwarded = forwardableHeaders(incoming);

    // The cookie jar belongs to THIS origin. FastAPI authenticates by
    // Authorization header, so a forwarded cookie is ignored at best
    // and logged upstream at worst -- and `authorization` is stripped
    // so a caller cannot supply its own bearer and bypass the cookie
    // as the single source of identity.
    expect(forwarded.get("cookie")).toBeNull();
    expect(forwarded.get("authorization")).toBeNull();

    // Hop-by-hop headers describe one connection and would corrupt the
    // next one.
    expect(forwarded.get("host")).toBeNull();
    expect(forwarded.get("content-length")).toBeNull();
    expect(forwarded.get("accept-encoding")).toBeNull();

    // Everything else survives, or the proxy would be useless.
    expect(forwarded.get("content-type")).toBe("application/json");
    expect(forwarded.get("x-documind-csrf")).toBe("1");
  });
});

test.describe("the proxy allowlist", () => {
  test("U5. only named backend paths resolve", () => {
    // Allowed.
    expect(resolveBackendPath(["users", "me"])).toEqual({
      backendPath: "/api/v1/users/me",
    });
    expect(resolveBackendPath(["documents"])).toEqual({
      backendPath: "/api/v1/documents",
    });
    expect(resolveBackendPath(["health"])).toEqual({ backendPath: "/health" });

    // Not allowed. A new backend route is unreachable until someone
    // adds it here on purpose.
    expect(resolveBackendPath(["secrets"])).toBeNull();
    expect(resolveBackendPath(["admin", "shutdown"])).toBeNull();
    expect(resolveBackendPath([])).toBeNull();

    // `auth` is DELIBERATELY absent: the backend's /auth/login responds
    // with {access_token}, so proxying it would hand the token to the
    // browser and undo the httpOnly cookie in one word.
    expect(resolveBackendPath(["auth", "login"])).toBeNull();

    // Traversal-ish segments are refused rather than normalised.
    expect(resolveBackendPath(["documents", ".."])).toBeNull();
    expect(resolveBackendPath(["", "documents"])).toBeNull();
  });

  test("U5. and the route actually applies it", async ({ page }) => {
    await registerAndLogin(page);

    const statuses = await page.evaluate(async () => {
      const get = async (p: string) => (await fetch(p)).status;
      return {
        allowed: await get("/api/documents"),
        unknown: await get("/api/secrets"),
        admin: await get("/api/admin/shutdown"),
      };
    });

    expect(statuses.allowed).toBe(200);
    // 404, not 403: a 403 would confirm the path exists upstream.
    expect(statuses.unknown).toBe(404);
    expect(statuses.admin).toBe(404);
  });
});

test.describe("client IP attestation", () => {
  test("U10. a forged client-IP pair from the browser is stripped", () => {
    // The backend TRUSTS these when the secret matches, so a browser
    // able to set them would choose its own rate-limit bucket -- a
    // fresh empty one per request, which is strictly worse than no
    // per-IP limit because the limit would still look enforced.
    //
    // forwardableHeaders runs BEFORE the route sets the real pair.
    // Strip then set, in that order, is the whole defence.
    const incoming = new Headers({
      "x-client-ip": "1.2.3.4",
      "x-client-ip-secret": "guessed-the-secret",
      "content-type": "application/json",
    });

    const forwarded = forwardableHeaders(incoming);

    expect(forwarded.get("x-client-ip")).toBeNull();
    expect(forwarded.get("x-client-ip-secret")).toBeNull();
    expect(forwarded.get("content-type")).toBe("application/json");
  });

  test("U11. the platform header is preferred over the spoofable ones", () => {
    // Vercel's request-headers reference says of x-forwarded-for: "we
    // currently overwrite the X-Forwarded-For header and do not forward
    // external IPs. This restriction is in place to prevent IP
    // spoofing." But custom X-Forwarded-For is purchasable as a Trusted
    // Proxy on Enterprise plans, and the docs note x-vercel-forwarded-for
    // is "identical to the x-forwarded-for header. However,
    // x-forwarded-for could be overwritten if you're using a proxy on
    // top of Vercel."
    //
    // So x-vercel-forwarded-for is the one that stays correct if a CDN
    // is ever put in front, and the precedence must reflect that.
    const all = new Headers({
      "x-vercel-forwarded-for": "203.0.113.7",
      "x-real-ip": "198.51.100.1",
      "x-forwarded-for": "10.0.0.1",
    });
    expect(platformClientIp(all)).toBe("203.0.113.7");

    // Leftmost entry: the list is client, then proxies.
    expect(
      platformClientIp(new Headers({ "x-forwarded-for": "203.0.113.7, 10.0.0.1" })),
    ).toBe("203.0.113.7");

    // Nothing to attest means nothing is sent, rather than a guess.
    expect(platformClientIp(new Headers())).toBeNull();

    // Capped: the value becomes part of a Redis key upstream.
    expect(
      platformClientIp(new Headers({ "x-real-ip": "2001:db8:".padEnd(200, "0") })),
    ).toBeNull();
  });
});
