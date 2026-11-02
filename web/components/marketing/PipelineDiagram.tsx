/**
 * The retrieval pipeline, drawn.
 *
 * Inline SVG, no library: it is six boxes and some arrows, and a chart
 * dependency would be more code than the picture. It uses token
 * colours via `currentColor` and CSS variables so it retheme with
 * everything else, and it carries a <title> plus role="img" so a
 * screen reader gets the sequence rather than silence.
 *
 * The horizontal layout is wrapped in an overflow-x container: a wide
 * diagram must scroll inside its own box rather than making the page
 * scroll sideways.
 */

type Stage = { label: string; sub: string; gate?: boolean };

const STAGES: readonly Stage[] = [
  { label: "Your question", sub: "plain language" },
  { label: "Two searches", sub: "meaning + words" },
  { label: "Merge", sub: "combined shortlist" },
  { label: "Re-read", sub: "careful reorder" },
  { label: "Good enough?", sub: "or say so", gate: true },
  { label: "Answer", sub: "written + cited" },
] as const;

export function PipelineDiagram() {
  const boxW = 140;
  const boxH = 74;
  const gap = 30;
  const width = STAGES.length * boxW + (STAGES.length - 1) * gap;
  // Tall enough for the refusal branch that drops BELOW the gate box.
  // Sized from the parts rather than a literal, so moving the boxes
  // cannot clip the caption again.
  const top = 20;
  const height = top + boxH + 58;

  return (
    <div className="overflow-x-auto rounded-card border border-line bg-s1/50 p-6">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-labelledby="pipeline-title pipeline-desc"
        className="min-w-[720px]"
      >
        <title id="pipeline-title">The retrieval pipeline</title>
        <desc id="pipeline-desc">
          Your question runs two searches at once, one matching on meaning and one
          on the exact words. Their results are merged into a shortlist, re-read
          and reordered by a more careful model, then checked: if the best match is
          too weak the system says it could not find an answer, otherwise it writes
          an answer with citations.
        </desc>

        <defs>
          <linearGradient id="pipe-accent" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="var(--ac)" />
            <stop offset="100%" stopColor="var(--amber)" />
          </linearGradient>
          <marker
            id="pipe-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M0 0 L10 5 L0 10 z" fill="var(--line2)" />
          </marker>
        </defs>

        {STAGES.map((stage, i) => {
          const x = i * (boxW + gap);
          return (
            <g key={stage.label}>
              {i > 0 && (
                <line
                  x1={x - gap + 4}
                  y1={top + boxH / 2}
                  x2={x - 6}
                  y2={top + boxH / 2}
                  stroke="var(--line2)"
                  strokeWidth="1.5"
                  markerEnd="url(#pipe-arrow)"
                />
              )}
              <rect
                x={x}
                y={top}
                width={boxW}
                height={boxH}
                rx="12"
                fill="var(--s2)"
                stroke={stage.gate ? "url(#pipe-accent)" : "var(--line)"}
                strokeWidth={stage.gate ? 1.6 : 1}
              />
              <text
                x={x + boxW / 2}
                y={top + 30}
                textAnchor="middle"
                fill="var(--tx)"
                fontSize="13.5"
                fontWeight="600"
                fontFamily="var(--font-sora), system-ui, sans-serif"
              >
                {stage.label}
              </text>
              <text
                x={x + boxW / 2}
                y={top + 50}
                textAnchor="middle"
                fill="var(--tx3)"
                fontSize="11.5"
                fontFamily="var(--font-inter), system-ui, sans-serif"
              >
                {stage.sub}
              </text>
            </g>
          );
        })}

        {/* The refusal branch, dropping out of the gate. */}
        <g>
          <path
            d={`M ${4 * (boxW + gap) + boxW / 2} ${top + boxH} V ${top + boxH + 26}`}
            stroke="var(--line2)"
            strokeWidth="1.5"
            strokeDasharray="4 4"
            fill="none"
            markerEnd="url(#pipe-arrow)"
          />
          <text
            x={4 * (boxW + gap) + boxW / 2}
            y={top + boxH + 44}
            textAnchor="middle"
            fill="var(--tx2)"
            fontSize="11.5"
            fontFamily="var(--font-inter), system-ui, sans-serif"
          >
            &quot;I could not find this&quot;
          </text>
        </g>
      </svg>
    </div>
  );
}
