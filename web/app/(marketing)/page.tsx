import type { Metadata } from "next";
import Link from "next/link";

import { AnswerDemo } from "@/components/marketing/AnswerDemo";
import { Reveal } from "@/components/marketing/Reveal";
import { features, paymentRoadmap, planOutlines, site, trustPoints } from "@/lib/site";

export const metadata: Metadata = {
  title: `${site.name} — ${site.tagline}`,
  description: site.description,
};

/* The hero orbs. Blurred, drifting, behind everything, decorative. */
function HeroAtmosphere() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div
        className="animate-drift-slow absolute -left-32 -top-40 h-[32rem] w-[32rem] rounded-full blur-[110px]"
        style={{ background: "radial-gradient(circle, rgba(255,107,0,.20), transparent 68%)" }}
      />
      <div
        className="animate-drift-slower absolute -right-28 top-10 h-[26rem] w-[26rem] rounded-full blur-[110px]"
        style={{ background: "radial-gradient(circle, rgba(255,176,32,.14), transparent 68%)" }}
      />
      {/* A faint grid, masked to fade out before the edges so it reads
          as texture rather than as a table. */}
      <div
        className="absolute inset-0 opacity-[0.6]"
        style={{
          backgroundImage:
            "linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px)",
          backgroundSize: "56px 56px",
          maskImage:
            "radial-gradient(ellipse 70% 55% at 50% 30%, #000 20%, transparent 75%)",
          WebkitMaskImage:
            "radial-gradient(ellipse 70% 55% at 50% 30%, #000 20%, transparent 75%)",
        }}
      />
    </div>
  );
}

export default function LandingPage() {
  return (
    <>
      {/* ---------------------------------------------------- hero ---- */}
      <section className="relative overflow-hidden">
        <HeroAtmosphere />
        <div className="relative mx-auto grid w-full max-w-[1200px] items-center gap-12 px-6 py-20 sm:px-8 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:py-28">
          <div>
            <p className="animate-fade-up inline-flex items-center gap-2 rounded-pill border border-line bg-s1/70 px-3 py-1 font-mono text-[11px] text-tx2 backdrop-blur">
              <span className="h-1.5 w-1.5 rounded-full bg-ok" />
              Multi-tenant retrieval, built to be checked
            </p>

            <h1 className="animate-fade-up stagger-1 mt-5 font-display text-[40px] font-semibold leading-[1.08] tracking-display text-tx sm:text-[54px]">
              Ask your documents.
              <br />
              Get answers you can{" "}
              <span className="ac-text">verify</span>.
            </h1>

            <p className="animate-fade-up stagger-2 mt-5 max-w-[56ch] text-[16px] leading-relaxed text-tx2">
              {site.subcopy}
            </p>

            <div className="animate-fade-up stagger-3 mt-8 flex flex-wrap items-center gap-3">
              <Link
                href="/login"
                className="ac-surface group relative inline-flex h-12 items-center gap-2 overflow-hidden rounded-ctl px-6 text-[15px] font-semibold text-on-ac transition-all duration-200 ease-brand hover:-translate-y-0.5 hover:shadow-[0_14px_38px_rgba(255,107,0,0.45)] motion-reduce:hover:translate-y-0"
              >
                Sign in
                <svg viewBox="0 0 20 20" className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5" aria-hidden="true">
                  <path d="M4 10h11M11 5.5 15.5 10 11 14.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </Link>
              <Link
                href="/how-it-works"
                className="inline-flex h-12 items-center rounded-ctl border border-line px-5 text-[15px] text-tx2 transition-all duration-200 ease-brand hover:border-line2 hover:bg-s2 hover:text-tx"
              >
                See how it works
              </Link>
            </div>

            <ul className="animate-fade-up stagger-4 mt-12 grid gap-5 sm:grid-cols-3">
              {trustPoints.map((point) => (
                <li key={point.title}>
                  <p className="text-[13.5px] font-semibold text-tx">{point.title}</p>
                  <p className="mt-1 text-[12.5px] leading-relaxed text-tx3">{point.body}</p>
                </li>
              ))}
            </ul>
          </div>

          <div className="animate-fade-up stagger-2 lg:pl-4">
            <AnswerDemo />
          </div>
        </div>
      </section>

      {/* ------------------------------------------------ features ---- */}
      <section className="mx-auto w-full max-w-[1200px] px-6 py-20 sm:px-8">
        <Reveal>
          <h2 className="font-display text-[30px] font-semibold tracking-display text-tx">
            Built around one idea
          </h2>
          <p className="mt-3 max-w-[62ch] text-[15px] leading-relaxed text-tx2">
            A language model will answer anything you put in front of it, fluently,
            whether or not the material supports the answer. Everything here exists
            to make that failure visible instead of invisible.
          </p>
        </Reveal>

        <ul className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {features.map((feature, i) => (
            <Reveal as="li" key={feature.title} delay={i * 60}>
              <div className="group h-full rounded-card border border-line bg-s1/60 p-6 transition-all duration-200 ease-brand hover:-translate-y-0.5 hover:border-line2 hover:bg-s1 motion-reduce:hover:translate-y-0">
                <div className="h-0.5 w-8 rounded-pill bg-[linear-gradient(90deg,var(--ac),var(--amber))] transition-all duration-300 ease-brand group-hover:w-14" />
                <h3 className="mt-4 text-[15px] font-semibold text-tx">{feature.title}</h3>
                <p className="mt-2 text-[13.5px] leading-relaxed text-tx2">{feature.body}</p>
              </div>
            </Reveal>
          ))}
        </ul>
      </section>

      {/* ------------------------------------------------- billing ---- */}
      <section className="border-y border-line bg-s1/40">
        <div className="mx-auto w-full max-w-[1200px] px-6 py-20 sm:px-8">
          <Reveal>
            <span className="rounded-pill border border-[color:var(--amber)]/30 bg-[color:var(--amber)]/10 px-3 py-1 font-mono text-[11px] text-[color:var(--amber)]">
              Roadmap
            </span>
            <h2 className="mt-4 font-display text-[30px] font-semibold tracking-display text-tx">
              {paymentRoadmap.heading}
            </h2>
            <p className="mt-3 max-w-[62ch] text-[15px] leading-relaxed text-tx2">
              {paymentRoadmap.body}
            </p>
          </Reveal>

          <div className="mt-10 grid gap-4 lg:grid-cols-[1fr_1fr]">
            <Reveal className="grid gap-4 sm:grid-cols-2">
              {paymentRoadmap.providers.map((provider) => (
                <div
                  key={provider.name}
                  className="rounded-card border border-dashed border-line bg-s2/40 p-6"
                >
                  <div className="flex items-center justify-between gap-3">
                    <h3 className="text-[15px] font-semibold text-tx">{provider.name}</h3>
                    <span className="rounded-pill border border-line px-2 py-0.5 font-mono text-[10px] text-tx3">
                      {provider.available ? "Available" : "Not yet"}
                    </span>
                  </div>
                  <p className="mt-2 text-[13px] leading-relaxed text-tx2">{provider.note}</p>
                </div>
              ))}
            </Reveal>

            <Reveal delay={80} className="grid gap-3">
              {planOutlines.map((plan) => (
                <div
                  key={plan.name}
                  className="flex items-start gap-4 rounded-card border border-line bg-s1 p-5"
                >
                  <span className="mt-0.5 shrink-0 rounded-ctl border border-line px-2.5 py-1 font-mono text-[11px] text-ac2">
                    {plan.name}
                  </span>
                  <p className="text-[13px] leading-relaxed text-tx2">{plan.body}</p>
                </div>
              ))}
              <p className="text-[12px] text-tx3">
                No prices are shown because none have been set, and nothing in this
                product charges anyone today.
              </p>
            </Reveal>
          </div>
        </div>
      </section>

      {/* ----------------------------------------------------- cta ---- */}
      <section className="mx-auto w-full max-w-[1200px] px-6 py-24 sm:px-8">
        <Reveal className="relative overflow-hidden rounded-card border border-line bg-s1 px-8 py-16 text-center">
          <div
            aria-hidden="true"
            className="animate-drift-slow pointer-events-none absolute left-1/2 top-0 h-80 w-[40rem] -translate-x-1/2 blur-[90px]"
            style={{ background: "radial-gradient(circle, rgba(255,107,0,.16), transparent 70%)" }}
          />
          <div className="relative">
            <h2 className="font-display text-[32px] font-semibold tracking-display text-tx">
              Put it against your own documents
            </h2>
            <p className="mx-auto mt-3 max-w-[54ch] text-[15px] leading-relaxed text-tx2">
              The only evaluation that matters is whether it answers your questions
              from your corpus — and tells you when it cannot.
            </p>
            <div className="mt-8 flex flex-wrap justify-center gap-3">
              <Link
                href="/login"
                className="ac-surface inline-flex h-12 items-center rounded-ctl px-6 text-[15px] font-semibold text-on-ac transition-all duration-200 ease-brand hover:-translate-y-0.5 hover:shadow-[0_14px_38px_rgba(255,107,0,0.45)] motion-reduce:hover:translate-y-0"
              >
                Sign in
              </Link>
              <Link
                href="/how-its-built"
                className="inline-flex h-12 items-center rounded-ctl border border-line px-5 text-[15px] text-tx2 transition-all duration-200 ease-brand hover:border-line2 hover:bg-s2 hover:text-tx"
              >
                Read the architecture
              </Link>
            </div>
          </div>
        </Reveal>
      </section>
    </>
  );
}
