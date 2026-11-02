import { AppShell } from "@/components/app/AppShell";
import { UserProvider } from "@/components/app/UserContext";
import { getHealthStatus, requireUser } from "@/lib/server-api";

/**
 * The signed-in application shell.
 *
 * A route GROUP, so "(app)" never appears in a URL. It exists to put
 * the real authentication check in ONE place: every page underneath it
 * is behind `requireUser`, which asks the backend rather than trusting
 * the cookie's presence. proxy.ts also redirects, but that is UX --
 * a cookie can be present and its token expired, revoked, or for a
 * user who has since been deleted.
 */
export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Both are server-side reads that happen before first paint, so the
  // shell renders with a real user and a real status rather than
  // flashing placeholders.
  const [user, healthStatus] = await Promise.all([
    requireUser(),
    getHealthStatus(),
  ]);

  return (
    <UserProvider user={user}>
      <AppShell healthStatus={healthStatus}>{children}</AppShell>
    </UserProvider>
  );
}
