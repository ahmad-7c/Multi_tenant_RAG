"use client";

import { cx } from "@/lib/cx";
import type { Citation } from "@/lib/types";

/**
 * The slide-in Sources panel.
 *
 * Each card shows the citation number, file, page, a relevance bar
 * (only when the score is real), and the parent passage the model
 * actually read -- with the matched child highlighted inside it.
 *
 * "Open in document" is NOT here. Serving a document's bytes needs a
 * short-lived signed URL that does not exist yet (see the backlog in
 * CLAUDE.md); a link that 404s would be worse than its absence.
 */

/**
 * Highlight the matched child inside the parent passage.
 *
 * Plain substring search, no regex: the needle is document text and
 * would need escaping, and a mis-escaped pattern is a crash on
 * somebody's PDF. If the child does not appear verbatim -- children
 * are overlapping token windows and can split mid-sentence -- the
 * passage renders unhighlighted rather than guessing.
 */
function HighlightedPassage({
  passage,
  matched,
}: {
  passage: string;
  matched: string | null;
}) {
  if (matched) {
    const needle = matched.trim();
    const at = needle ? passage.indexOf(needle) : -1;
    if (at !== -1) {
      return (
        <p className="text-[13px] leading-relaxed text-tx2">
          {passage.slice(0, at)}
          <mark className="rounded-[3px] bg-ac-tint px-0.5 text-tx">
            {passage.slice(at, at + needle.length)}
          </mark>
          {passage.slice(at + needle.length)}
        </p>
      );
    }
  }
  return <p className="text-[13px] leading-relaxed text-tx2">{passage}</p>;
}

function RelevanceBar({ score }: { score: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, score)) * 100);
  return (
    <div className="mt-3">
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-tx3">relevance</span>
        <span className="font-mono text-tx2">{score.toFixed(2)}</span>
      </div>
      <div
        role="meter"
        aria-label="Relevance"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        className="mt-1 h-1 w-full overflow-hidden rounded-pill bg-s3"
      >
        <div
          className="progress-fill h-full rounded-pill bg-[linear-gradient(90deg,var(--ac),var(--amber))]"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function SourcesPanel({
  citations,
  activeMarker,
  onSelect,
  onClose,
  open,
}: {
  citations: Citation[];
  activeMarker: number | null;
  onSelect: (n: number) => void;
  onClose: () => void;
  open: boolean;
}) {
  if (!open || citations.length === 0) return null;

  return (
    <aside
      aria-label="Sources"
      className="animate-fade-up flex w-full shrink-0 flex-col border-t border-line bg-s1/50 xl:w-[380px] xl:border-l xl:border-t-0"
    >
      <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-4">
        <h2 className="font-display text-[15px] font-semibold text-tx">
          Sources
          <span className="ml-2 font-mono text-[12px] font-normal text-tx3">
            {citations.length}
          </span>
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close sources"
          className="inline-flex h-8 w-8 items-center justify-center rounded-ctl border border-line text-tx3 transition-colors duration-200 hover:text-tx"
        >
          <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <div className="flex flex-col gap-3 overflow-y-auto p-4">
        {citations.map((citation) => {
          const active = citation.marker === activeMarker;
          return (
            <button
              key={citation.marker}
              type="button"
              onClick={() => onSelect(citation.marker)}
              className={cx(
                "rounded-card p-4 text-left transition-all duration-200 ease-brand",
                // The ONE focal animated element on this screen.
                active
                  ? "conic-border"
                  : "border border-line bg-s1 hover:border-line2 hover:bg-s2",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-[6px] bg-ac-tint font-mono text-[11px] text-ac2">
                  {citation.marker}
                </span>
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-tx">
                  {citation.document}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-tx3">
                  p.{citation.page}
                </span>
              </div>

              {/* Only when the score is a real relevance. Null means
                  the reranker fell back or was a passthrough, where the
                  underlying number is a rank reciprocal. */}
              {citation.score !== null && <RelevanceBar score={citation.score} />}

              <div className="mt-3 border-t border-line pt-3">
                <HighlightedPassage
                  passage={citation.passage ?? citation.snippet}
                  matched={null}
                />
              </div>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
