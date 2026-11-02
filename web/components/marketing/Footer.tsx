import Link from "next/link";

import { Logo } from "@/components/marketing/Logo";
import { site, topNav } from "@/lib/site";

export function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-6 px-6 py-10 sm:flex-row sm:items-center sm:justify-between sm:px-8">
        <div className="flex flex-col gap-2">
          <Logo size={24} />
          <p className="max-w-[46ch] text-[13px] text-tx3">{site.description}</p>
        </div>
        <nav aria-label="Footer" className="flex flex-wrap items-center gap-5">
          {topNav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="text-[13px] text-tx2 transition-colors duration-200 hover:text-tx"
            >
              {item.label}
            </Link>
          ))}
          <Link
            href="/login"
            className="text-[13px] text-ac2 transition-colors duration-200 hover:text-amber"
          >
            Sign in
          </Link>
        </nav>
      </div>
    </footer>
  );
}
