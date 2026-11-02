"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

/**
 * A sidebar link. Active state is an orange tint plus a 3px indicator
 * on the sidebar edge.
 *
 * A real <Link> (so it is an anchor): middle-click, ctrl-click and
 * "open in new tab" all work, and the browser handles focus. A div
 * with a router.push would silently break all three.
 */
export function NavItem({
  href,
  icon,
  children,
}: {
  href: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  const pathname = usePathname();
  // startsWith so /documents/abc keeps /documents highlighted, but
  // guarded with a boundary check so /usage never matches /usage-foo.
  const active = pathname === href || pathname.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      // The programmatic signal for "you are here". Without it the
      // orange tint is the only cue and a screen reader gets nothing.
      aria-current={active ? "page" : undefined}
      className={cx(
        "relative flex items-center gap-3 rounded-ctl px-3 py-2.5",
        "text-[14px] transition-all duration-200 ease-brand",
        active
          ? "bg-ac-tint text-tx"
          : "text-tx2 hover:bg-s2 hover:text-tx",
      )}
    >
      {active && (
        <span
          aria-hidden="true"
          className="absolute -left-4 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-pill bg-ac"
        />
      )}
      <span className={cx("shrink-0", active ? "text-ac2" : "text-tx3")}>
        {icon}
      </span>
      {children}
    </Link>
  );
}
