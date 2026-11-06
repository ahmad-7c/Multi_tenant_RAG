"use client";

import { usePathname } from "next/navigation";
import { useState } from "react";

import { MenuButton, Sidebar } from "@/components/app/Sidebar";
import { useUser } from "@/components/app/UserContext";
import { StatusDot } from "@/components/ui/chips";
import { ToastHost } from "@/components/ui/Toast";

const PAGE_TITLES: Record<string, string> = {
  "/documents": "Documents",
  "/chat": "Chat",
  "/usage": "Usage",
};

function titleFor(pathname: string): string {
  const match = Object.keys(PAGE_TITLES).find(
    (href) => pathname === href || pathname.startsWith(`${href}/`),
  );
  return match ? PAGE_TITLES[match]! : "";
}

/**
 * The status chip.
 *
 * `status` is the overall word from /health and nothing else. The
 * `degraded[]` list is dropped server-side (see lib/server-api.ts) --
 * it names internal subsystems and is operator detail, not something
 * to show a user mid-question.
 */
function StatusChipInline({ status }: { status: string | null }) {
  const ok = status === "ok";
  return (
    <span className="inline-flex items-center gap-2 rounded-pill border border-line bg-s1 px-3 py-1.5 text-[12px] text-tx2">
      <StatusDot tone={ok ? "ok" : "warn"} ping={ok} />
      {/* The dot alone below `sm`. On a 390px header the breadcrumb and
          a six-word chip cannot both fit, and the breadcrumb is what
          tells you where you are. `sr-only sm:not-sr-only` keeps the
          wording available to assistive tech at every width -- ONE
          element, so the two cannot drift apart. Two spans (one
          `hidden sm:inline`, one `sr-only sm:hidden`) said the same
          thing twice and a reworded chip would have been reworded in
          only one of them. */}
      <span className="sr-only sm:not-sr-only">
        {ok ? "All systems normal" : status ? "Service degraded" : "Status unknown"}
      </span>
    </span>
  );
}

export function AppShell({
  healthStatus,
  children,
}: {
  healthStatus: string | null;
  children: React.ReactNode;
}) {
  const user = useUser();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const title = titleFor(pathname);

  return (
    <ToastHost>
      <div className="flex min-h-screen">
        <Sidebar open={menuOpen} onClose={() => setMenuOpen(false)} />

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-[72px] shrink-0 items-center justify-between gap-4 border-b border-line px-5 sm:px-8">
            <div className="flex min-w-0 items-center gap-3">
              <MenuButton onClick={() => setMenuOpen(true)} />
              {/* Breadcrumb: org, then page. The org name is not on the
                  user payload, so the email's domain-free local part is
                  not guessed at -- the org id is shown nowhere and the
                  crumb starts at the page. */}
              <nav aria-label="Breadcrumb" className="min-w-0">
                <ol className="flex items-center gap-2 text-[14px]">
                  <li className="hidden truncate text-tx3 sm:block">
                    {user.email.split("@")[1] ?? "Organization"}
                  </li>
                  <li aria-hidden="true" className="hidden text-tx3 sm:block">
                    /
                  </li>
                  <li className="truncate font-medium text-tx">{title}</li>
                </ol>
              </nav>
            </div>
            <StatusChipInline status={healthStatus} />
          </header>

          {/* min-w-0 so a wide table inside can scroll in its own box
              instead of stretching the page. */}
          <main className="min-w-0 flex-1">{children}</main>
        </div>
      </div>
    </ToastHost>
  );
}
