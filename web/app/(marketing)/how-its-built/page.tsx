import type { Metadata } from "next";
import Link from "next/link";

import { Reveal } from "@/components/marketing/Reveal";
import { architectureSections, site, techStack } from "@/lib/site";

export const metadata: Metadata = {
  title: `How it's built — ${site.name}`,
  description:
    "The engineering behind DocuMind: tenant isolation, parent-child chunking, hybrid retrieval, the confidence gate, prompt safety and operational failure modes.",
};

export default function HowItsBuiltPage() {
  return (
    <>
      <section className="mx-auto w-full max-w-[1200px] px-6 pb-12 pt-20 sm:px-8">
        <Reveal>
          <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-ac2">
            How it&apos;s built
          </p>
          <h1 className="mt-4 max-w-[22ch] font-display text-[40px] font-semibold leading-[1.1] tracking-display text-tx sm:text-[48px]">
            The decisions, and what they were chosen{" "}
            <span className="ac-text">over</span>
          </h1>
          <p className="mt-5 max-w-[66ch] text-[16px] leading-relaxed text-tx2">
            A list of technologies says nothing — most systems pick from the same
            shelf. What distinguishes them is the alternative that was rejected and
            why. Each decision below is stated with its rejected alternative.
            Looking for the plain-language version?{" "}
            <Link href="/how-it-works" className="text-ac2 underline underline-offset-4 hover:no-underline">
              How it works
            </Link>
            .
          </p>
        </Reveal>

        {/* Jump links. A long technical page needs a way in. */}
        <Reveal delay={60}>
          <nav aria-label="Sections" className="mt-9 flex flex-wrap gap-2">
            {architectureSections.map((section) => (
              <a
                key={section.id}
                href={`#${section.id}`}
                className="rounded-pill border border-line bg-s1/60 px-3.5 py-1.5 text-[13px] text-tx2 transition-all duration-200 ease-brand hover:border-line2 hover:bg-s2 hover:text-tx"
              >
                {section.title}
              </a>
            ))}
          </nav>
        </Reveal>
      </section>

      <section className="mx-auto w-full max-w-[1200px] px-6 pb-16 sm:px-8">
        <div className="flex flex-col gap-14">
          {architectureSections.map((section) => (
            <Reveal
              as="article"
              key={section.id}
              /* scroll-mt clears the sticky 72px top bar, or a jump link
                 lands with the heading hidden underneath it. */
              className="scroll-mt-24"
            >
              <div id={section.id} className="border-t border-line pt-8">
                <h2 className="font-display text-[26px] font-semibold tracking-display text-tx">
                  {section.title}
                </h2>
                <p className="mt-2.5 max-w-[70ch] text-[15px] leading-relaxed text-tx2">
                  {section.lead}
                </p>

                <dl className="mt-7 grid gap-3 lg:grid-cols-2">
                  {section.points.map((point) => (
                    <div
                      key={point.k}
                      className="group rounded-card border border-line bg-s1/60 p-5 transition-all duration-200 ease-brand hover:-translate-y-0.5 hover:border-line2 hover:bg-s1 motion-reduce:hover:translate-y-0"
                    >
                      <dt className="flex items-start gap-2.5 text-[14px] font-semibold text-tx">
                        <span
                          aria-hidden="true"
                          className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-ac transition-all duration-300 ease-brand group-hover:w-5"
                        />
                        {point.k}
                      </dt>
                      <dd className="mt-2 pl-[18px] text-[13.5px] leading-relaxed text-tx2">
                        {point.v}
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ----------------------------------------------------- stack ---- */}
      <section className="border-t border-line bg-s1/40">
        <div className="mx-auto w-full max-w-[1200px] px-6 py-16 sm:px-8">
          <Reveal>
            <h2 className="font-display text-[24px] font-semibold tracking-display text-tx">
              The stack
            </h2>
            <p className="mt-2 max-w-[62ch] text-[14px] text-tx2">
              Named without versions, because versions move and the choices above
              are what actually matter.
            </p>
          </Reveal>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {techStack.map((group, i) => (
              <Reveal key={group.group} delay={i * 50}>
                <div className="h-full rounded-card border border-line bg-s1 p-5">
                  <h3 className="font-mono text-[11px] uppercase tracking-[0.16em] text-ac2">
                    {group.group}
                  </h3>
                  <ul className="mt-3 flex flex-wrap gap-1.5">
                    {group.items.map((item) => (
                      <li
                        key={item}
                        className="rounded-pill border border-line bg-s2 px-2.5 py-1 text-[12.5px] text-tx2"
                      >
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------- testing ---- */}
      <section className="mx-auto w-full max-w-[1200px] px-6 py-20 sm:px-8">
        <Reveal className="rounded-card border border-line bg-s1/60 p-8">
          <h2 className="font-display text-[22px] font-semibold tracking-display text-tx">
            How these claims are kept true
          </h2>
          <p className="mt-3 max-w-[72ch] text-[14.5px] leading-relaxed text-tx2">
            The test suite runs against a real PostgreSQL with the vector extension
            rather than a substitute, because every claim above is a claim about
            database behaviour — index types, tenant filters, ranking — and none of
            it is observable on a stand-in.
          </p>
          <p className="mt-3 max-w-[72ch] text-[14.5px] leading-relaxed text-tx2">
            Beyond that, the suite is checked by{" "}
            <strong className="text-tx">mutation testing</strong>: a plausible bug
            is introduced on purpose — a tenant filter dropped, a score exposed
            when it is not comparable, a cached entry left readable — and the run
            must fail. A test that passes against a broken system is not evidence.
            That exercise has already found tests that were named after a failure
            mode without exercising it.
          </p>
        </Reveal>
      </section>
    </>
  );
}
