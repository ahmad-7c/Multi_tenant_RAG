import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

/* ------------------------------------------------------------------ *
 * StatusChip
 *
 * Always an icon AND text. Colour alone fails for the ~8% of men with
 * a colour-vision deficiency, and "ready" vs "failed" is exactly the
 * distinction they would lose.
 * ------------------------------------------------------------------ */

export type Status = "ready" | "processing" | "queued" | "failed";

const statusStyles: Record<Status, string> = {
  ready: "text-ok-tx border-[color:var(--ok)]/30 bg-[color:var(--ok)]/10",
  processing: "text-ac2 border-[color:var(--ac)]/30 bg-ac-tint",
  queued: "text-tx2 border-line bg-s2",
  failed: "text-err-tx border-[color:var(--err)]/30 bg-[color:var(--err)]/10",
};

const statusLabels: Record<Status, string> = {
  ready: "Ready",
  processing: "Processing",
  queued: "Queued",
  failed: "Failed",
};

function StatusIcon({ status }: { status: Status }) {
  if (status === "processing") {
    return (
      <svg
        viewBox="0 0 16 16"
        className="h-3 w-3 animate-spin motion-reduce:animate-none"
        aria-hidden="true"
      >
        <circle
          cx="8"
          cy="8"
          r="6"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray="28"
          strokeDashoffset="10"
        />
      </svg>
    );
  }
  if (status === "queued") {
    return (
      <svg viewBox="0 0 16 16" className="h-3 w-3" aria-hidden="true">
        <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 5v3.5l2 1.2" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
      </svg>
    );
  }
  if (status === "failed") {
    return (
      <svg viewBox="0 0 16 16" className="h-3 w-3" aria-hidden="true">
        <path d="M8 1.8 15 14H1z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        <path d="M8 6.2v3.1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <circle cx="8" cy="11.4" r="0.8" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 16 16" className="h-3 w-3" aria-hidden="true">
      <path d="M3 8.5l3.2 3.2L13 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function StatusChip({ status }: { status: Status }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1",
        "text-[12px] font-medium",
        statusStyles[status],
      )}
    >
      <StatusIcon status={status} />
      {statusLabels[status]}
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * CitationChip -- the clickable [n] inside an answer.
 * ------------------------------------------------------------------ */

export function CitationChip({
  n,
  active,
  onClick,
}: {
  n: number;
  active?: boolean;
  onClick?: () => void;
}) {
  // A button, not a span: it opens the source panel, so it must be
  // reachable by keyboard and announced as actionable.
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Show source ${n}`}
      className={cx(
        "inline-flex h-5 w-5 items-center justify-center align-super",
        "rounded-[6px] font-mono text-[11px] leading-none",
        "transition-all duration-200 ease-brand hover:-translate-y-px",
        active
          ? "bg-ac text-on-ac"
          : "bg-ac-tint text-ac2 hover:bg-[color:var(--ac)]/25",
      )}
    >
      {n}
    </button>
  );
}

/* ------------------------------------------------------------------ *
 * SourcePill -- citation chip plus "file.pdf, p.12".
 * ------------------------------------------------------------------ */

export function SourcePill({
  n,
  file,
  page,
  onClick,
}: {
  n: number;
  file: string;
  page: number;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cx(
        "inline-flex items-center gap-2 rounded-pill border border-line",
        "bg-s2 py-1 pl-1.5 pr-3 text-[12px] text-tx2",
        "transition-all duration-200 ease-brand",
        "hover:border-line2 hover:bg-s3 hover:text-tx",
      )}
    >
      <span className="inline-flex h-5 w-5 items-center justify-center rounded-[6px] bg-ac-tint font-mono text-[11px] leading-none text-ac2">
        {n}
      </span>
      <span className="max-w-[18ch] truncate">{file}</span>
      <span className="font-mono text-tx3">p.{page}</span>
    </button>
  );
}

/* ------------------------------------------------------------------ *
 * TraceChip
 *
 * Only ever rendered from a real backend field. `value` is required
 * and the component does NOT accept undefined -- the CALLER must omit
 * the chip when the backend sent null, rather than this rendering a
 * dash or a zero. That is the data-honesty rule made structural: there
 * is no way to pass "no value" to this component.
 * ------------------------------------------------------------------ */

export function TraceChip({
  label,
  value,
  tone = "neutral",
  title,
}: {
  label: string;
  value: string;
  tone?: "neutral" | "good" | "accent";
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-pill border px-2 py-0.5",
        "font-mono text-[11px]",
        tone === "good" && "border-[color:var(--ok)]/25 text-ok-tx",
        tone === "accent" && "border-[color:var(--ac)]/25 text-ac2",
        tone === "neutral" && "border-line text-tx3",
      )}
    >
      <span className="text-tx3">{label}</span>
      <span className={tone === "neutral" ? "text-tx2" : undefined}>
        {value}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * StatusDot -- green dot with an expanding ping for live states.
 * ------------------------------------------------------------------ */

export function StatusDot({
  tone = "ok",
  ping = true,
}: {
  tone?: "ok" | "warn" | "err";
  ping?: boolean;
}) {
  const colour =
    tone === "ok"
      ? "var(--ok)"
      : tone === "warn"
        ? "var(--amber)"
        : "var(--err)";
  return (
    <span className="relative inline-flex h-2 w-2" aria-hidden="true">
      {ping && (
        <span
          className="absolute inset-0 rounded-full animate-ping-dot motion-reduce:hidden"
          style={{ background: colour }}
        />
      )}
      <span
        className="relative inline-flex h-2 w-2 rounded-full"
        style={{ background: colour }}
      />
    </span>
  );
}

/* ------------------------------------------------------------------ *
 * Avatar -- orange-to-amber gradient with dark initials.
 * ------------------------------------------------------------------ */

export function Avatar({ name, size = 32 }: { name: string; size?: number }) {
  const initials = name
    .split(/[\s-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
  return (
    <span
      className="inline-flex shrink-0 items-center justify-center rounded-ctl font-semibold text-on-ac"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.4),
        background: "linear-gradient(135deg, var(--ac), var(--amber))",
      }}
      aria-hidden="true"
    >
      {initials || "?"}
    </span>
  );
}

export function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-pill border border-line bg-s2 px-2.5 py-1 text-[12px] text-tx2">
      {children}
    </span>
  );
}
