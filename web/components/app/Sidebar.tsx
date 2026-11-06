"use client";

import { useState } from "react";

import { Logo } from "@/components/marketing/Logo";
import { useSignOut, useUser } from "@/components/app/UserContext";
import { Avatar } from "@/components/ui/chips";
import { NavItem } from "@/components/ui/NavItem";
import { cx } from "@/lib/cx";

const DocumentsIcon = (
  <svg viewBox="0 0 20 20" className="h-[18px] w-[18px]" aria-hidden="true">
    <path d="M5 2.5h6l4 4v11H5z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M11 2.5v4h4" fill="none" stroke="currentColor" strokeWidth="1.5" />
  </svg>
);

const ChatIcon = (
  <svg viewBox="0 0 20 20" className="h-[18px] w-[18px]" aria-hidden="true">
    <path d="M3 5.5h14v8.5H9.5L6 17v-3H3z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
  </svg>
);

const UsageIcon = (
  <svg viewBox="0 0 20 20" className="h-[18px] w-[18px]" aria-hidden="true">
    <path d="M3 16.5h14M6 16.5v-5M10 16.5V5M14 16.5v-8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);

const SignOutIcon = (
  <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
    <path d="M8 17H4.5v-14H8M13 13.5 16.5 10 13 6.5M8 10h8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

function Nav({ isAdmin }: { isAdmin: boolean }) {
  return (
    <nav aria-label="Sections" className="flex flex-col gap-1">
      <NavItem href="/documents" icon={DocumentsIcon}>
        Documents
      </NavItem>
      <NavItem href="/chat" icon={ChatIcon}>
        Chat
      </NavItem>
      {/* Admin only, and the backend enforces it too: a member who
          types /usage gets a 403 from the API regardless of what the
          sidebar chose to render. Hiding a control is a courtesy, not
          a permission check. */}
      {isAdmin && (
        <NavItem href="/usage" icon={UsageIcon}>
          Usage
        </NavItem>
      )}
    </nav>
  );
}

function Account() {
  const user = useUser();
  const signOut = useSignOut();
  const [pending, setPending] = useState(false);

  return (
    <div className="flex items-center gap-3 border-t border-line px-4 py-4">
      <Avatar name={user.email} size={34} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium text-tx">{user.email}</p>
        <p className="font-mono text-[11px] capitalize text-tx3">{user.role}</p>
      </div>
      <button
        type="button"
        aria-label="Sign out"
        title="Sign out"
        disabled={pending}
        onClick={() => {
          setPending(true);
          void signOut();
        }}
        className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-ctl border border-line text-tx3 transition-all duration-200 ease-brand hover:border-line2 hover:bg-s2 hover:text-tx disabled:opacity-50"
      >
        {SignOutIcon}
      </button>
    </div>
  );
}

/**
 * 256px rail on desktop; a slide-over on narrow screens.
 *
 * The budget meter the design spec places at the bottom is NOT here:
 * there is no endpoint that reports budget usage, and the Redis
 * counters cannot be read back. Omitted at checkpoint C0 rather than
 * filled with a plausible number.
 */
export function Sidebar({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const user = useUser();
  const isAdmin = user.role === "admin";

  return (
    <>
      {/* Desktop rail. */}
      <aside className="hidden w-[256px] shrink-0 flex-col border-r border-line bg-s1/60 lg:flex">
        <div className="px-5 py-5">
          <Logo />
        </div>
        <div className="flex-1 px-4">
          <Nav isAdmin={isAdmin} />
        </div>
        <Account />
      </aside>

      {/* Mobile slide-over. */}
      {open && (
        <div className="fixed inset-0 z-50 lg:hidden">
          {/* Tap-outside-to-close. aria-hidden because the drawer now
              has a real Close button; exposing both would put two
              identical controls in the accessibility tree. */}
          <div
            aria-hidden="true"
            onClick={onClose}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          />
          <aside className="animate-fade-up absolute inset-y-0 left-0 flex w-[270px] flex-col border-r border-line bg-s1">
            <div className="flex items-center justify-between gap-3 px-5 py-5">
              <Logo />
              {/* A VISIBLE close control, not just the backdrop.
                  Tapping outside is a convention, not an affordance:
                  on a 390px screen this drawer covers most of the
                  view, and a sighted touch user needs something to
                  aim at. 44px so it is a legal tap target. */}
              <button
                type="button"
                onClick={onClose}
                aria-label="Close menu"
                className="-mr-2 inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-ctl text-tx2 transition-colors duration-200 hover:bg-s2 hover:text-tx"
              >
                <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
                  <path
                    d="M5 5l10 10M15 5L5 15"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
            <div className="flex-1 px-4" onClick={onClose}>
              <Nav isAdmin={isAdmin} />
            </div>
            <Account />
          </aside>
        </div>
      )}
    </>
  );
}

export function MenuButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label="Open menu"
      onClick={onClick}
      className={cx(
        "inline-flex h-10 w-10 items-center justify-center rounded-ctl",
        "border border-line text-tx2 lg:hidden",
      )}
    >
      <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
        <path d="M3 6h14M3 10h14M3 14h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    </button>
  );
}
