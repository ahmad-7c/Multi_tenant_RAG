import type { Metadata } from "next";
import Link from "next/link";

import { PipelineDiagram } from "@/components/marketing/PipelineDiagram";
import { Reveal } from "@/components/marketing/Reveal";
import { howItWorksSteps, site } from "@/lib/site";

export const metadata: Metadata = {
  title: `How it works — ${site.name}`,
  description:
    "A plain-language walkthrough of how DocuMind turns a question about your documents into an answer you can check.",
};

export default function HowItWorksPage() {
  return (
    <>
      <section className="mx-auto w-full max-w-[1200px] px-6 pb-10 pt-20 sm:px-8">
        <Reveal>
          <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-ac2">
            How it works
          </p>
          <h1 className="mt-4 max-w-[20ch] font-display text-[40px] font-semibold leading-[1.1] tracking-display text-tx sm:text-[48px]">
            From a question to an answer you can{" "}
            <span className="ac-text">check</span>
          </h1>
          <p className="mt-5 max-w-[64ch] text-[16px] leading-relaxed text-tx2">
            No jargon on this page. If you want the engineering — the index types,
            the ranking maths, the isolation guarantees — that is on{" "}
            <Link href="/how-its-built" className="text-ac2 underline underline-offset-4 hover:no-underline">
              How it&apos;s built
            </Link>
            .
          </p>
        </Reveal>
      </section>

      <section className="mx-auto w-full max-w-[1200px] px-6 pb-14 sm:px-8">
        <Reveal>
          <PipelineDiagram />
        </Reveal>
      </section>

      <section className="mx-auto w-full max-w-[1200px] px-6 pb-24 sm:px-8">
        <ol className="relative flex flex-col gap-5">
          {/* The spine. Hidden on small screens where the steps stack
              edge to edge and a connector has nothing to connect. */}
          <span
            aria-hidden="true"
            className="absolute bottom-10 left-[26px] top-10 hidden w-px bg-[linear-gradient(180deg,transparent,var(--line2),transparent)] sm:block"
          />
          {howItWorksSteps.map((step, i) => (
            <Reveal as="li" key={step.n} delay={i * 50} className="relative">
              <div className="flex gap-5">
                <span className="relative z-10 hidden h-[54px] w-[54px] shrink-0 items-center justify-center rounded-card border border-line bg-s1 font-display text-[18px] font-semibold text-ac2 sm:flex">
                  {step.n}
                </span>
                <div className="flex-1 rounded-card border border-line bg-s1/60 p-6 transition-all duration-200 ease-brand hover:border-line2 hover:bg-s1">
                  <div className="flex items-baseline gap-3">
                    <span className="font-display text-[15px] font-semibold text-ac2 sm:hidden">
                      {step.n}.
                    </span>
                    <h2 className="font-display text-[18px] font-semibold text-tx">
                      {step.title}
                    </h2>
                  </div>
                  <p className="mt-2.5 text-[15px] leading-relaxed text-tx">
                    {step.plain}
                  </p>
                  <p className="mt-3 border-l-2 border-[color:var(--ac)]/30 pl-4 text-[13.5px] leading-relaxed text-tx2">
                    {step.detail}
                  </p>
                </div>
              </div>
            </Reveal>
          ))}
        </ol>

        <Reveal delay={80} className="mt-12 rounded-card border border-[color:var(--amber)]/25 bg-[color:var(--amber)]/[0.06] p-6">
          <h2 className="font-display text-[17px] font-semibold text-[color:var(--amber)]">
            The honest limitation
          </h2>
          <p className="mt-2 max-w-[70ch] text-[14px] leading-relaxed text-tx2">
            A citation proves the text was retrieved and shown to the model. It does
            not prove the answer follows from it. Checking that a reference is real
            is mechanical and we do it on every answer; judging whether a claim is
            actually supported needs a human reading the passage — which is exactly
            why the passage is put in front of you instead of hidden behind a link.
          </p>
        </Reveal>
      </section>
    </>
  );
}
