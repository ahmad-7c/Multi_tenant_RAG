"use client";

import { useEffect, useRef, useState } from "react";

import { CitationChip, SourcePill, TraceChip } from "@/components/ui/chips";
import { usePrefersReducedMotion } from "@/lib/hooks";

/**
 * The hero's animated demo card: a question types itself, trace chips
 * appear, an answer streams in, citations land.
 *
 * ILLUSTRATIVE, and labelled as such on the card. The numbers are
 * fixed script values, not measurements — the rule against inventing
 * statistics is about presenting a figure as if the system produced
 * it, so the card says "illustration" where a reader can see it.
 *
 * The script is a plain data structure so the timing is readable in
 * one place rather than spread across nested setTimeouts.
 */

const QUESTION = "How soon must expense reports be filed after a trip?";
const ANSWER =
  "Expense reports must be submitted within thirty days of the end of travel. Reports filed after that window need written approval from a department director.";

// Phase boundaries in ms from mount.
const TYPE_MS = 28; // per character
const AFTER_TYPING = 420;
const AFTER_TRACE = 520;
const STREAM_MS = 14; // per character

export function AnswerDemo() {
  const reduced = usePrefersReducedMotion();
  // Initialised to the FINAL state when motion is reduced, rather than
  // animated-from-empty and then corrected inside an effect. The
  // correction version renders an empty card first and fixes it a tick
  // later, which is a flash for exactly the people who asked for less
  // movement.
  const [typed, setTyped] = useState(() => (reduced ? QUESTION : ""));
  const [phase, setPhase] = useState<"typing" | "trace" | "streaming" | "done">(
    () => (reduced ? "done" : "typing"),
  );
  const [streamed, setStreamed] = useState(() => (reduced ? ANSWER : ""));
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  useEffect(() => {
    if (reduced) return;

    const push = (fn: () => void, ms: number) => {
      timers.current.push(setTimeout(fn, ms));
    };

    let at = 300;
    for (let i = 1; i <= QUESTION.length; i += 1) {
      push(() => setTyped(QUESTION.slice(0, i)), at);
      at += TYPE_MS;
    }
    at += AFTER_TYPING;
    push(() => setPhase("trace"), at);
    at += AFTER_TRACE;
    push(() => setPhase("streaming"), at);
    for (let i = 1; i <= ANSWER.length; i += 1) {
      push(() => setStreamed(ANSWER.slice(0, i)), at);
      at += STREAM_MS;
    }
    push(() => setPhase("done"), at);

    const handles = timers.current;
    // Every timer is cleared on unmount. Without this, a user who
    // navigates away mid-animation leaves ~300 pending setState calls
    // pointing at an unmounted component.
    return () => handles.forEach(clearTimeout);
  }, [reduced]);

  const showTrace = phase !== "typing";
  const showAnswer = phase === "streaming" || phase === "done";

  return (
    <div className="conic-border rounded-card p-5 sm:p-6">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-tx3">
          Illustration
        </span>
        <span className="rounded-pill border border-line px-2 py-0.5 font-mono text-[10px] text-tx3">
          Org only
        </span>
      </div>

      {/* The question, typed. */}
      <div className="mt-4 flex justify-end">
        <p className="ac-surface max-w-[85%] rounded-card rounded-br-[6px] px-3.5 py-2.5 text-[13.5px] font-medium text-on-ac">
          {typed}
          {phase === "typing" && (
            <span className="animate-caret ml-0.5 inline-block w-[2px] align-middle" style={{ height: "1em", background: "var(--on-ac)" }} />
          )}
        </p>
      </div>

      {/* Trace chips, then the answer. */}
      <div
        className={`mt-4 transition-opacity duration-500 ease-brand ${showTrace ? "opacity-100" : "opacity-0"}`}
      >
        <div className="flex flex-wrap gap-1.5">
          <TraceChip label="retrieve" value="289 ms" />
          <TraceChip label="rerank" value="118 ms" />
          <TraceChip label="confidence" value="0.87" tone="good" />
        </div>

        <div
          className={`mt-3 transition-opacity duration-500 ease-brand ${showAnswer ? "opacity-100" : "opacity-0"}`}
        >
          <p className="text-[13.5px] leading-relaxed text-tx">
            {streamed}
            {phase === "streaming" && (
              <span className="animate-caret ml-0.5 inline-block h-[1em] w-[2px] align-middle bg-ac2" />
            )}
            {phase === "done" && (
              <>
                {" "}
                <CitationChip n={1} active />
              </>
            )}
          </p>

          <div
            className={`mt-3.5 flex flex-wrap gap-2 transition-all duration-500 ease-brand ${
              phase === "done"
                ? "translate-y-0 opacity-100"
                : "translate-y-1 opacity-0"
            }`}
          >
            <SourcePill n={1} file="travel-policy.md" page={1} />
          </div>
        </div>
      </div>
    </div>
  );
}
