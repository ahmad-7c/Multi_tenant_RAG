import type { InputHTMLAttributes, ReactNode } from "react";

import { cx } from "@/lib/cx";

/* ------------------------------------------------------------------ *
 * Input -- 46px, s2 surface, orange focus ring.
 * ------------------------------------------------------------------ */

export function Input({
  label,
  hint,
  error,
  id,
  className,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  error?: string;
}) {
  // A real <label for>, always. A placeholder is not a label: it
  // vanishes on focus, is invisible to a screen reader as a name, and
  // leaves the user unable to check what they typed into which field.
  const inputId = id ?? `in-${label.toLowerCase().replace(/\s+/g, "-")}`;
  const describedBy = error
    ? `${inputId}-error`
    : hint
      ? `${inputId}-hint`
      : undefined;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-[13px] font-medium text-tx2">
        {label}
      </label>
      <input
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cx(
          "h-[46px] w-full rounded-ctl bg-s2 px-3.5 text-[15px] text-tx",
          "border border-line placeholder:text-tx3",
          "transition-all duration-200 ease-brand",
          "focus:border-ac2 focus:outline-none focus:ring-4 focus:ring-ac-ring",
          error && "border-[color:var(--err)]",
          className,
        )}
        {...rest}
      />
      {error ? (
        <p id={`${inputId}-error`} className="text-[12px] text-err-tx">
          {error}
        </p>
      ) : hint ? (
        <p id={`${inputId}-hint`} className="text-[12px] text-tx3">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Card
 * ------------------------------------------------------------------ */

export function Card({
  children,
  className,
  interactive,
}: {
  children: ReactNode;
  className?: string;
  interactive?: boolean;
}) {
  return (
    <div
      className={cx(
        "rounded-card border border-line bg-s1 p-[22px]",
        interactive &&
          "transition-all duration-200 ease-brand hover:-translate-y-0.5 " +
            "hover:border-line2 motion-reduce:hover:translate-y-0",
        className,
      )}
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * ProgressBar
 *
 * `value` is optional ON PURPOSE: undefined means indeterminate. The
 * backend's DocumentStatus has no percentage, so an ingesting document
 * genuinely has no number -- and a fake 60% would be an invented
 * statistic. Indeterminate is the honest rendering.
 * ------------------------------------------------------------------ */

export function ProgressBar({
  value,
  label,
}: {
  value?: number;
  label: string;
}) {
  const indeterminate = value === undefined;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={indeterminate ? undefined : Math.round(value)}
      aria-valuemin={indeterminate ? undefined : 0}
      aria-valuemax={indeterminate ? undefined : 100}
      className="h-1.5 w-full overflow-hidden rounded-pill bg-s3"
    >
      <div
        className={cx(
          "h-full rounded-pill",
          indeterminate
            ? "w-1/3 animate-shimmer bg-[linear-gradient(90deg,transparent,var(--ac),var(--amber),transparent)]"
            : "progress-fill bg-[linear-gradient(90deg,var(--ac),var(--amber))]",
        )}
        style={indeterminate ? undefined : { width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Segmented control
 * ------------------------------------------------------------------ */

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: Array<{ value: T; label: string }>;
  value: T;
  onChange: (next: T) => void;
  label: string;
}) {
  return (
    // A radiogroup, not a row of buttons: arrow keys work and a screen
    // reader announces "2 of 3 selected" rather than three separate
    // unrelated controls.
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex gap-1 rounded-ctl border border-line bg-s1 p-1"
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(option.value)}
            className={cx(
              "rounded-[7px] px-3 py-1.5 text-[13px] font-medium",
              "transition-all duration-200 ease-brand",
              selected
                ? "bg-ac-tint text-ac2"
                : "text-tx3 hover:bg-s2 hover:text-tx2",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * InlineBanner / EmptyState / Skeleton
 * ------------------------------------------------------------------ */

export function InlineBanner({
  tone,
  title,
  children,
}: {
  tone: "info" | "error";
  title: string;
  children?: ReactNode;
}) {
  return (
    <div
      // assertive for errors: the user needs to know the action failed
      // before they try again. polite for info, which can wait for a
      // pause in what the screen reader is already saying.
      role={tone === "error" ? "alert" : "status"}
      className={cx(
        "rounded-card border p-4",
        tone === "error"
          ? "border-[color:var(--err)]/30 bg-[color:var(--err)]/8"
          : "border-[color:var(--amber)]/25 bg-[color:var(--amber)]/8",
      )}
    >
      <p
        className={cx(
          "text-[14px] font-semibold",
          tone === "error" ? "text-err-tx" : "text-[color:var(--amber)]",
        )}
      >
        {title}
      </p>
      {children && <div className="mt-1 text-[13px] text-tx2">{children}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-card border border-dashed border-line px-6 py-14 text-center">
      {/* A heading, not a styled paragraph. An empty state is the only
          thing on the screen when it shows, so it is that region's
          title -- and a screen-reader user navigating by headings
          would otherwise skip straight past the one piece of text
          telling them why the page is blank. */}
      <h2 className="font-display text-[16px] font-semibold text-tx">{title}</h2>
      {children && <p className="max-w-[44ch] text-[13px] text-tx2">{children}</p>}
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cx(
        "animate-shimmer rounded-ctl",
        "bg-[linear-gradient(90deg,var(--s2),var(--s3),var(--s2))]",
        className,
      )}
    />
  );
}
