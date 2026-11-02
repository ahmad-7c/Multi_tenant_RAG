import { site } from "@/lib/site";

/**
 * The mark plus wordmark.
 *
 * The glyph is inline SVG rather than an image file: it inherits
 * currentColor, needs no network request, and cannot be blocked by the
 * image CSP directive.
 */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <span className="inline-flex items-center gap-2.5">
      <span
        className="ac-surface inline-flex shrink-0 items-center justify-center rounded-[9px]"
        style={{ width: size, height: size }}
        aria-hidden="true"
      >
        <svg
          viewBox="0 0 24 24"
          width={size * 0.6}
          height={size * 0.6}
          fill="none"
          stroke="var(--on-ac)"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          {/* A page with a magnifier: document plus retrieval. */}
          <path d="M6 3h7l5 5v3" />
          <path d="M13 3v5h5" />
          <path d="M6 3v18h5" />
          <circle cx="16.5" cy="16.5" r="3.5" />
          <path d="M19.2 19.2 22 22" />
        </svg>
      </span>
      <span className="font-display text-[17px] font-semibold tracking-display text-tx">
        {site.name}
      </span>
    </span>
  );
}
