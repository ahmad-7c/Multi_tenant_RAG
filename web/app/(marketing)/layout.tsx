import { Footer } from "@/components/marketing/Footer";
import { TopBar } from "@/components/marketing/TopBar";

/**
 * Chrome for the public pages.
 *
 * A route GROUP -- the (marketing) directory name is not part of any
 * URL. It exists so the landing page, "How it works" and "How it's
 * built" share one top bar and footer without the signed-in app
 * inheriting them: the app has a sidebar, not a marketing header.
 */
export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-col">
      <TopBar />
      <main className="flex-1">{children}</main>
      <Footer />
    </div>
  );
}
