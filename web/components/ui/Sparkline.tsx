import { cx } from "@/lib/cx";

/**
 * A sparkline drawn on mount with stroke-dashoffset.
 *
 * `pathLength={1}` normalises the geometry so the single `draw`
 * keyframe (dashoffset 1 -> 0) works for any series length -- without
 * it the dash array would have to be computed per path in JS.
 *
 * Renders nothing for fewer than two points. A one-point "trend" is
 * not a trend, and a flat line drawn from a single value reads as
 * "nothing changed" rather than "there is no series yet".
 */
export function Sparkline({
  points,
  label,
  className,
  width = 120,
  height = 36,
}: {
  points: number[];
  label: string;
  className?: string;
  width?: number;
  height?: number;
}) {
  if (points.length < 2) return null;

  const max = Math.max(...points);
  const min = Math.min(...points);
  // A flat series would divide by zero; draw it down the middle.
  const span = max - min || 1;
  const stepX = width / (points.length - 1);
  // 1px inset top and bottom so a 2px stroke is not clipped at the
  // extremes.
  const inset = 2;
  const usable = height - inset * 2;

  const d = points
    .map((value, index) => {
      const x = index * stepX;
      const y = inset + usable - ((value - min) / span) * usable;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cx("overflow-visible", className)}
      role="img"
      aria-label={label}
    >
      <path
        d={d}
        pathLength={1}
        fill="none"
        stroke="var(--ac)"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="animate-draw"
      />
    </svg>
  );
}
