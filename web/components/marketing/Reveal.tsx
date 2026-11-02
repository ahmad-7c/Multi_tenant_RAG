"use client";

import { useEffect, useRef, useState } from "react";

import { cx } from "@/lib/cx";
import { usePrefersReducedMotion } from "@/lib/hooks";

/**
 * Fades its children up when they scroll into view, once.
 *
 * An IntersectionObserver rather than a scroll handler: the browser
 * decides when to tell us, off the main thread, instead of us asking
 * on every scroll event. The observer DISCONNECTS after firing, so a
 * long page does not accumulate live observers as the user scrolls.
 *
 * The content is always in the DOM and the server-rendered HTML is the
 * FINAL state -- a reader without JavaScript, or a crawler, sees the
 * page rather than a blank column. The hidden state is applied only
 * once the observer is actually watching.
 */
export function Reveal({
  children,
  delay = 0,
  className,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  delay?: number;
  className?: string;
  as?: "div" | "section" | "li" | "article";
}) {
  const ref = useRef<HTMLElement | null>(null);
  const reduced = usePrefersReducedMotion();
  // `armed` is set by the effect, which is a legitimate setState in an
  // effect: it records that an external subscription now exists, not a
  // value derived from props.
  const [armed, setArmed] = useState(false);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const node = ref.current;
    // No point arming an animation the stylesheet is about to
    // neutralise anyway.
    if (!node || reduced) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          observer.disconnect();
        }
      },
      // A negative bottom margin fires it slightly BEFORE the element
      // reaches the viewport edge, so the motion has finished by the
      // time it is properly in view.
      { rootMargin: "0px 0px -12% 0px", threshold: 0.05 },
    );
    observer.observe(node);
    setArmed(true);
    return () => observer.disconnect();
  }, [reduced]);

  const hidden = armed && !shown && !reduced;

  return (
    <Tag
      ref={ref as never}
      style={hidden ? undefined : { transitionDelay: `${delay}ms` }}
      className={cx(
        "transition-all duration-700 ease-brand motion-reduce:transition-none",
        hidden ? "translate-y-4 opacity-0" : "translate-y-0 opacity-100",
        className,
      )}
    >
      {children}
    </Tag>
  );
}
