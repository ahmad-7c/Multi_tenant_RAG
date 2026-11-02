import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cx } from "@/lib/cx";

type Variant = "primary" | "ghost" | "danger";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  children: ReactNode;
};

/*
 * Always a real <button>. Never a styled <div> with onClick: a div is
 * not focusable, is not in the tab order, does not fire on Enter or
 * Space, and is announced as nothing by a screen reader.
 */
const base =
  "inline-flex items-center justify-center gap-2 h-11 px-4 rounded-ctl " +
  "text-[14px] font-medium transition-all duration-200 ease-brand " +
  "disabled:opacity-50 disabled:pointer-events-none select-none";

const variants: Record<Variant, string> = {
  // Dark text on orange: ~6.9:1. White on orange fails AA and is never
  // used. The lift + glow + sweep is the one signature hover.
  primary:
    "relative overflow-hidden bg-ac text-on-ac font-semibold " +
    "hover:-translate-y-0.5 hover:shadow-[0_10px_30px_rgba(255,107,0,0.5)] " +
    "active:translate-y-0 " +
    "before:absolute before:inset-0 before:-translate-x-full " +
    "before:bg-[linear-gradient(90deg,transparent,rgba(255,255,255,0.35),transparent)] " +
    "hover:before:translate-x-full before:transition-transform " +
    "before:duration-500 before:ease-brand motion-reduce:before:hidden " +
    "motion-reduce:hover:translate-y-0",
  ghost:
    "bg-transparent text-tx2 border border-line hover:text-tx " +
    "hover:border-line2 hover:bg-s2",
  // Status red, used only for destructive actions.
  danger:
    "bg-transparent text-err-tx border border-[color:var(--err)]/40 " +
    "hover:bg-[color:var(--err)]/10 hover:border-[color:var(--err)]",
};

export function Button({ variant = "primary", className, ...rest }: Props) {
  return (
    <button
      type={rest.type ?? "button"}
      className={cx(base, variants[variant], className)}
      {...rest}
    />
  );
}

/**
 * Icon-only button. 34px on desktop, but 44px minimum on touch, which
 * is the accessibility floor for a tap target -- an icon button is the
 * easiest control to make too small to hit.
 */
export function IconButton({
  label,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type={rest.type ?? "button"}
      // The only text a screen reader gets, since the child is a glyph.
      aria-label={label}
      title={label}
      className={cx(
        "inline-flex items-center justify-center rounded-ctl text-tx2",
        "h-11 w-11 sm:h-[34px] sm:w-[34px]",
        "border border-line transition-all duration-200 ease-brand",
        "hover:text-tx hover:border-line2 hover:bg-s2",
        "disabled:opacity-50 disabled:pointer-events-none",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
