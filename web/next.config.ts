import type { NextConfig } from "next";

/**
 * Security headers that do not need a per-request value.
 *
 * Content-Security-Policy is deliberately NOT here: it carries a
 * per-request nonce and so lives in proxy.ts. A static CSP in this file
 * would be overwritten by the proxy's, or worse, would need
 * 'unsafe-inline' to work at all.
 */
const securityHeaders = [
  // Stops a browser from second-guessing a declared Content-Type --
  // the vector where a text/plain upload gets sniffed as script.
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Send the full URL only to our own origin. Document names and
  // conversation ids live in paths here, so a cross-origin Referer
  // would leak tenant content through a link click.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // This app needs none of these. Denied rather than left to the
  // browser's default, which varies.
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=()",
  },
  // frame-ancestors in the CSP is the modern control and is already
  // set; this is the legacy header for browsers that only honour it.
  { key: "X-Frame-Options", value: "DENY" },
];

const nextConfig: NextConfig = {
  // Fail the build on a type error rather than shipping one. Explicit
  // because the flag that disables this is a common "just get it
  // deployed" edit, and a BFF is the wrong place for that.
  //
  // There is no `eslint` key here: Next 16 removed it from NextConfig
  // along with `next lint`. Linting runs as its own `npm run lint`
  // step instead of inside the build.
  typescript: { ignoreBuildErrors: false },
  // The framework version is a free hint to anyone fingerprinting.
  poweredByHeader: false,
  // Tailwind v4 runs as a Turbopack CSS loader. create-next-app
  // installed @tailwindcss/turbopack but did NOT add this rule, so the
  // first build emitted the raw stylesheet with an "Unknown at rule:
  // @theme" warning and ZERO utility classes. A warning, not an error,
  // so the build went green while every `bg-s1` and `rounded-card` in
  // the app resolved to nothing.
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async headers() {
    return [{ source: "/(.*)", headers: securityHeaders }];
  },
};

export default nextConfig;
