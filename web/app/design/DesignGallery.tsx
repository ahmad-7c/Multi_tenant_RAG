"use client";

import { useState } from "react";

import { Button, IconButton } from "@/components/ui/Button";
import {
  Avatar,
  Chip,
  CitationChip,
  SourcePill,
  StatusChip,
  StatusDot,
  TraceChip,
} from "@/components/ui/chips";
import { NavItem } from "@/components/ui/NavItem";
import {
  Card,
  EmptyState,
  InlineBanner,
  Input,
  ProgressBar,
  Segmented,
  Skeleton,
} from "@/components/ui/primitives";
import { Sparkline } from "@/components/ui/Sparkline";
import { ToastHost, useToast } from "@/components/ui/Toast";

/*
 * Every component in every state, on one page. Sample values here are
 * LITERALS, not fetched data -- this page never calls the API, so there
 * is no risk of it presenting a real number out of context.
 */

function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4">
      <div>
        <h2 className="font-display text-[16px] font-semibold text-tx">
          {title}
        </h2>
        {note && <p className="mt-1 text-[13px] text-tx3">{note}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </section>
  );
}

function ToastDemo() {
  const toast = useToast();
  return (
    <>
      <Button variant="ghost" onClick={() => toast("info", "Document uploaded.")}>
        Show info toast
      </Button>
      <Button
        variant="ghost"
        onClick={() => toast("error", "Upload failed. The file is over 4 MB.")}
      >
        Show error toast
      </Button>
    </>
  );
}

const DocIcon = (
  <svg viewBox="0 0 20 20" className="h-[18px] w-[18px]" aria-hidden="true">
    <path
      d="M5 2.5h6l4 4v11H5z"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinejoin="round"
    />
    <path d="M11 2.5v4h4" fill="none" stroke="currentColor" strokeWidth="1.5" />
  </svg>
);

export function DesignGallery() {
  const [range, setRange] = useState<"1" | "7" | "30">("7");
  const [activeCitation, setActiveCitation] = useState(1);

  return (
    <ToastHost>
      <main className="mx-auto flex w-full max-w-[920px] flex-col gap-12 px-6 py-12 sm:px-10">
        <header className="animate-fade-up flex flex-col gap-2">
          <p className="font-mono text-[11px] uppercase tracking-widest text-ac2">
            dev only
          </p>
          <h1 className="font-display tracking-display text-[30px] font-semibold text-tx">
            Component reference
          </h1>
          <p className="text-[14px] text-tx2">
            Every base component and state. This page 404s in production.
          </p>
        </header>

        <Section title="Buttons" note="Primary hover: lift, orange glow, light sweep.">
          <Button>Sign in</Button>
          <Button disabled>Sign in</Button>
          <Button variant="ghost">Cancel</Button>
          <Button variant="danger">Delete document</Button>
          <IconButton label="Sign out">
            <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
              <path
                d="M8 17H4.5v-14H8M13 13.5 16.5 10 13 6.5M8 10h8"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </IconButton>
        </Section>

        <Section title="Inputs">
          {/*
            autoComplete="off" on every field here. Without it Chrome
            heuristically autofilled this gallery with the DEVELOPER'S
            OWN saved email and password -- harmless locally, but it put
            real credentials into a screenshot taken for design review.
            The real login form in U2 sets explicit, correct values
            (organization / username / current-password) rather than
            "off", because suppressing a password manager on a genuine
            sign-in form pushes people towards weaker passwords.
          */}
          <div className="grid w-full gap-4 sm:grid-cols-3">
            <Input label="Organization" placeholder="acme-corp" autoComplete="off" />
            <Input
              label="Email"
              type="email"
              hint="The address you registered."
              autoComplete="off"
            />
            <Input
              label="Password"
              type="password"
              error="Those details did not match an account."
              autoComplete="new-password"
            />
          </div>
        </Section>

        <Section title="Status chips" note="Always an icon and text, never colour alone.">
          <StatusChip status="ready" />
          <StatusChip status="processing" />
          <StatusChip status="queued" />
          <StatusChip status="failed" />
          <Chip>
            <StatusDot /> All systems normal
          </Chip>
          <Chip>
            <StatusDot tone="warn" ping={false} /> Degraded
          </Chip>
        </Section>

        <Section
          title="Trace chips"
          note="Rendered only from a real backend field. The component cannot accept a null value."
        >
          <TraceChip label="retrieve" value="289 ms" />
          <TraceChip label="rerank" value="3001 ms" />
          <TraceChip label="model first token" value="894 ms" />
          <TraceChip label="candidates" value="5" />
          <TraceChip label="confidence" value="0.87" tone="good" />
          <TraceChip label="from cache" value="yes" tone="accent" />
        </Section>

        <Section title="Citations and sources">
          <p className="w-full text-[15px] leading-relaxed text-tx">
            Expense reports must be submitted within thirty days of the end of
            travel{" "}
            <CitationChip
              n={1}
              active={activeCitation === 1}
              onClick={() => setActiveCitation(1)}
            />{" "}
            and late reports need director approval{" "}
            <CitationChip
              n={2}
              active={activeCitation === 2}
              onClick={() => setActiveCitation(2)}
            />
            .
          </p>
          <SourcePill n={1} file="travel-policy.md" page={1} />
          <SourcePill n={2} file="vendor-security-review.pdf" page={12} />
        </Section>

        <Section title="Avatars">
          <Avatar name="Acme Corp" />
          <Avatar name="Globex" size={40} />
        </Section>

        <Section title="Progress" note="Indeterminate when the backend reports no percentage.">
          <div className="flex w-full flex-col gap-5">
            <div className="flex flex-col gap-2">
              <span className="text-[12px] text-tx3">
                Indeterminate (processing a document)
              </span>
              <ProgressBar label="Processing document" />
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-[12px] text-tx3">Determinate (upload, 64%)</span>
              <ProgressBar label="Uploading" value={64} />
            </div>
          </div>
        </Section>

        <Section title="Segmented control">
          <Segmented
            label="Date range"
            value={range}
            onChange={setRange}
            options={[
              { value: "1", label: "Today" },
              { value: "7", label: "7 days" },
              { value: "30", label: "30 days" },
            ]}
          />
        </Section>

        <Section title="Sparkline" note="Drawn on mount. Renders nothing below two points.">
          <Sparkline label="Questions, last 7 days" points={[3, 9, 6, 14, 11, 19, 17]} />
          <span className="text-[12px] text-tx3">one point:</span>
          <Sparkline label="Single point" points={[4]} />
        </Section>

        <Section title="Cards">
          <div className="grid w-full gap-4 sm:grid-cols-2">
            <Card>
              <p className="text-[13px] text-tx2">Questions</p>
              <p className="font-display mt-1 text-[28px] font-semibold text-tx">
                1,284
              </p>
            </Card>
            <Card interactive>
              <p className="text-[13px] text-tx2">Interactive card</p>
              <p className="mt-1 text-[13px] text-tx3">Hovers up 2px.</p>
            </Card>
          </div>
        </Section>

        <Section
          title="Focal element"
          note="The animated conic border. At most ONE per screen; a static border under reduced motion."
        >
          <div className="conic-border w-full rounded-card p-5">
            <p className="text-[13px] text-tx2">
              Used on the login demo card and the active source card in chat.
            </p>
          </div>
        </Section>

        <Section title="Banners" note="A refusal is information, not an error.">
          <div className="flex w-full flex-col gap-3">
            <InlineBanner
              tone="info"
              title="I couldn't find this in your documents"
            >
              Try naming the document or policy you have in mind.
            </InlineBanner>
            <InlineBanner tone="error" title="The answer stopped early">
              The model took too long to respond. Ask again.
            </InlineBanner>
          </div>
        </Section>

        <Section title="Empty state">
          <div className="w-full">
            <EmptyState
              title="No documents yet"
              action={<Button>Upload document</Button>}
            >
              Upload a PDF, Markdown or text file and DocuMind will index it for
              questions.
            </EmptyState>
          </div>
        </Section>

        <Section title="Skeletons">
          <div className="flex w-full flex-col gap-2">
            <Skeleton className="h-5 w-1/3" />
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-5 w-1/2" />
          </div>
        </Section>

        <Section title="Navigation" note="Active: orange tint plus a 3px edge indicator.">
          <div className="w-[240px] rounded-card border border-line bg-s1 py-3 pl-4 pr-3">
            <nav className="flex flex-col gap-1">
              <NavItem href="/documents" icon={DocIcon}>
                Documents
              </NavItem>
              <NavItem href="/chat" icon={DocIcon}>
                Chat
              </NavItem>
              <NavItem href="/usage" icon={DocIcon}>
                Usage
              </NavItem>
            </nav>
          </div>
        </Section>

        <Section title="Toasts">
          <ToastDemo />
        </Section>

        <Section
          title="Not built"
          note="Omitted at checkpoint C0 because no backend field or endpoint provides the data."
        >
          <ul className="w-full list-disc space-y-1 pl-5 text-[13px] text-tx2">
            <li>
              <strong className="text-tx">BudgetMeter</strong> and{" "}
              <strong className="text-tx">Ring</strong> — the daily budget needs
              a new endpoint; the Redis counters cannot be read back today.
            </li>
            <li>
              <strong className="text-tx">Stage progress</strong> — status is a
              four-value enum with no stage and no percentage.
            </li>
          </ul>
        </Section>
      </main>
    </ToastHost>
  );
}
