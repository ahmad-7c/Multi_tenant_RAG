"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { Logo } from "@/components/marketing/Logo";
import { cx } from "@/lib/cx";
import { useScrolledPast } from "@/lib/hooks";
import { topNav } from "@/lib/site";

/**
 * The public top bar: wordmark left, three actions right.
 *
 * Links come from `topNav` in lib/site.ts, so adding a page is one
 * entry there rather than an edit here. "Sign in" is separate because
 * it is the primary action and is styled as one, not as a nav link.
 */
export function TopBar() {
  const pathname = usePathname();
  // The bar gains a border and a blur only once the page has moved, so
  // it is invisible against the hero and legible over content. Read
  // through an external-store hook rather than synced with an effect:
  // see lib/hooks.ts.
  const scrolled = useScrolledPast(8);
  const [open, setOpen] = useState(false);

  return (
    <header
      className={cx(
        "sticky top-0 z-40 transition-all duration-300 ease-brand",
        scrolled
          ? "border-b border-line bg-[color:var(--bg)]/80 backdrop-blur-xl"
          : "border-b border-transparent",
      )}
    >
      <nav
        aria-label="Main"
        className="mx-auto flex h-[72px] w-full max-w-[1200px] items-center justify-between gap-4 px-6 sm:px-8"
      >
        <Link href="/" className="rounded-ctl" aria-label={`${"DocuMind"} home`}>
          <Logo />
        </Link>

        <div className="flex items-center gap-1 sm:gap-2">
          <ul className="hidden items-center gap-1 md:flex">
            {topNav.map((item) => {
              const active = pathname === item.href;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={cx(
                      "relative rounded-ctl px-3.5 py-2 text-[14px]",
                      "transition-colors duration-200 ease-brand",
                      active ? "text-tx" : "text-tx2 hover:text-tx",
                    )}
                  >
                    {item.label}
                    <span
                      aria-hidden="true"
                      className={cx(
                        "absolute inset-x-3.5 -bottom-0.5 h-px origin-left",
                        "bg-[linear-gradient(90deg,var(--ac),var(--amber))]",
                        "transition-transform duration-300 ease-brand",
                        active ? "scale-x-100" : "scale-x-0",
                      )}
                    />
                  </Link>
                </li>
              );
            })}
          </ul>

          <Link
            href="/login"
            className={cx(
              "ac-surface relative inline-flex h-10 items-center justify-center",
              "overflow-hidden rounded-ctl px-4 text-[14px] font-semibold text-on-ac",
              "transition-all duration-200 ease-brand",
              "hover:-translate-y-0.5 hover:shadow-[0_10px_30px_rgba(255,107,0,0.45)]",
              "motion-reduce:hover:translate-y-0",
            )}
          >
            Sign in
          </Link>

          <button
            type="button"
            aria-label="Menu"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="inline-flex h-10 w-10 items-center justify-center rounded-ctl border border-line text-tx2 md:hidden"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
              <path
                d={open ? "M5 5l10 10M15 5L5 15" : "M3 6h14M3 10h14M3 14h14"}
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
      </nav>

      {open && (
        <ul className="animate-fade-up flex flex-col gap-1 border-t border-line bg-s1 px-6 py-3 md:hidden">
          {topNav.map((item) => (
            <li key={item.href}>
              <Link
                href={item.href}
                /* Closed by the click that navigates, not by an effect
                   watching the pathname afterwards. The click is the
                   actual cause; the pathname change is a consequence,
                   and reacting to the consequence means an extra
                   render with the menu still open over the new page. */
                onClick={() => setOpen(false)}
                className="block rounded-ctl px-3 py-3 text-[15px] text-tx2 hover:bg-s2 hover:text-tx"
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </header>
  );
}
