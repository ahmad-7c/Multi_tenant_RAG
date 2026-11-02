import Link from "next/link";
import { Suspense } from "react";

import { AnswerDemo } from "@/components/marketing/AnswerDemo";
import { Logo } from "@/components/marketing/Logo";
import { AuthForm } from "@/components/auth/AuthForm";
import { site, trustPoints } from "@/lib/site";

/**
 * Two columns: an animated hero on the left, the form on the right.
 *
 * The form column is `--s1` and fixed-width on desktop so it reads as
 * a panel rather than floating; the hero is fluid and absorbs the
 * remaining space.
 */
export function AuthLayout({ mode }: { mode: "login" | "register" }) {
  const isRegister = mode === "register";

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      {/* ------------------------------------------------ hero ------ */}
      <section className="relative flex flex-1 flex-col justify-center overflow-hidden px-6 py-14 sm:px-12 lg:py-20">
        <div aria-hidden="true" className="pointer-events-none absolute inset-0">
          <div
            className="animate-drift-slow absolute -left-24 top-0 h-[28rem] w-[28rem] rounded-full blur-[100px]"
            style={{ background: "radial-gradient(circle, rgba(255,107,0,.18), transparent 68%)" }}
          />
          <div
            className="animate-drift-slower absolute bottom-0 right-0 h-[22rem] w-[22rem] rounded-full blur-[100px]"
            style={{ background: "radial-gradient(circle, rgba(255,176,32,.12), transparent 68%)" }}
          />
          <div
            className="absolute inset-0"
            style={{
              backgroundImage:
                "linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px)",
              backgroundSize: "56px 56px",
              maskImage: "radial-gradient(ellipse 65% 60% at 40% 40%, #000 10%, transparent 72%)",
              WebkitMaskImage: "radial-gradient(ellipse 65% 60% at 40% 40%, #000 10%, transparent 72%)",
            }}
          />
        </div>

        <div className="relative mx-auto w-full max-w-[560px]">
          <Link href="/" className="animate-fade-up inline-block rounded-ctl">
            <Logo />
          </Link>

          <h1 className="animate-fade-up stagger-1 mt-8 font-display text-[32px] font-semibold leading-[1.12] tracking-display text-tx sm:text-[38px]">
            Ask your documents.
            <br />
            Get answers you can <span className="ac-text">verify</span>.
          </h1>
          <p className="animate-fade-up stagger-2 mt-4 max-w-[48ch] text-[14.5px] leading-relaxed text-tx2">
            {site.subcopy}
          </p>

          <div className="animate-fade-up stagger-3 mt-8">
            <AnswerDemo />
          </div>

          <ul className="animate-fade-up stagger-4 mt-8 grid gap-4 sm:grid-cols-3">
            {trustPoints.map((point) => (
              <li key={point.title}>
                <p className="text-[12.5px] font-semibold text-tx">{point.title}</p>
                <p className="mt-1 text-[11.5px] leading-relaxed text-tx3">
                  {point.body}
                </p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* ------------------------------------------------ panel ----- */}
      <section className="flex w-full shrink-0 items-center border-t border-line bg-s1 px-6 py-14 sm:px-12 lg:w-[520px] lg:border-l lg:border-t-0 lg:py-20">
        <div className="animate-fade-up mx-auto w-full max-w-[400px]">
          <h2 className="font-display text-[24px] font-semibold tracking-display text-tx">
            {isRegister ? "Create an organization" : "Welcome back"}
          </h2>
          <p className="mt-2 text-[14px] text-tx2">
            {isRegister
              ? "You will be the first admin. Your documents stay private to this organization."
              : "Sign in to your organization's workspace."}
          </p>

          <div className="mt-7">
            {/* useSearchParams needs a Suspense boundary in the App
                Router, or the whole route is forced client-side. */}
            <Suspense fallback={<div className="h-[332px]" />}>
              <AuthForm mode={mode} />
            </Suspense>
          </div>

          <p className="mt-6 text-[13.5px] text-tx2">
            {isRegister ? (
              <>
                Already have an organization?{" "}
                <Link href="/login" className="text-ac2 underline underline-offset-4 hover:no-underline">
                  Sign in
                </Link>
              </>
            ) : (
              <>
                New to {site.name}?{" "}
                <Link href="/register" className="text-ac2 underline underline-offset-4 hover:no-underline">
                  Create an organization
                </Link>
              </>
            )}
          </p>

          <p className="mt-8 border-t border-line pt-5 text-[12px] leading-relaxed text-tx3">
            Sign-in is scoped by organization, because the same email address may
            belong to accounts in more than one of them.
          </p>
        </div>
      </section>
    </div>
  );
}
