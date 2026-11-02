import type { Metadata } from "next";
import { Inter, JetBrains_Mono, Sora } from "next/font/google";

import { QueryProvider } from "@/components/QueryProvider";

import "./globals.css";

/*
 * next/font downloads and SELF-HOSTS these at build time, so there is
 * no request to fonts.gstatic.com at runtime. That is what lets the CSP
 * say `font-src 'self'` with no external host, and it removes a
 * render-blocking third-party request on first paint.
 *
 * Only the weights the design spec names are loaded. Pulling a whole
 * family costs bytes for weights nothing uses.
 */
const sora = Sora({
  variable: "--font-sora",
  subsets: ["latin"],
  weight: ["500", "600"],
  display: "swap",
});

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-mono-jb",
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

/*
 * Every page renders per request.
 *
 * Required by the nonce-based CSP: Next injects the nonce during
 * server-side rendering by reading the CSP header off the incoming
 * request, and a statically prerendered page was built when no request
 * existed. Those pages ship script tags with NO nonce, which the CSP
 * then blocks -- a blank screen in production and nothing wrong in dev.
 *
 * Worth being clear about the cost: this disables static optimization,
 * ISR and CDN caching of the HTML. It costs nothing real here, because
 * every screen behind /login is per-user and per-tenant and could never
 * have been cached anyway.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "DocuMind",
  description: "Ask your documents. Get answers you can verify.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${sora.variable} ${inter.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-full bg-bg text-tx flex flex-col">
        <QueryProvider>{children}</QueryProvider>
      </body>
    </html>
  );
}
