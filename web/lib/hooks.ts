"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * Reads of browser state that React does not own.
 *
 * All of these use `useSyncExternalStore` rather than
 * `useState` + `useEffect`. The effect version calls setState during
 * the effect body, which React 19 flags: it causes a second render
 * pass on every mount, and on a page of twenty revealing sections
 * that is twenty cascading re-renders before anything is visible.
 * `useSyncExternalStore` reads the value DURING render instead, so
 * there is one pass.
 *
 * It also gives a defined server snapshot, which matters here: these
 * components render on the server, where there is no `window`.
 */

const EMPTY = () => () => {};

/* ------------------------------------------------- reduced motion ---- */

function subscribeToMotionPreference(onChange: () => void) {
  if (typeof window === "undefined") return () => {};
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function readMotionPreference() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * True when the visitor has asked their OS to reduce motion.
 *
 * The server snapshot is `false` -- "motion is allowed" -- which
 * matches what the stylesheet assumes before its media query applies.
 * Returning `true` there would make every animated element render its
 * final state on the server and then jump into motion on hydration,
 * which is a flash for everyone rather than a courtesy for anyone.
 */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeToMotionPreference,
    readMotionPreference,
    () => false,
  );
}

/* -------------------------------------------------------- scrolled ---- */

function subscribeToScroll(onChange: () => void) {
  if (typeof window === "undefined") return () => {};
  // passive: this listener never calls preventDefault, and saying so
  // lets the browser keep scrolling on the compositor instead of
  // waiting to find out.
  window.addEventListener("scroll", onChange, { passive: true });
  return () => window.removeEventListener("scroll", onChange);
}

/**
 * True once the page has scrolled past `threshold` pixels.
 *
 * A boolean rather than the scroll offset on purpose: the snapshot is
 * compared by identity on every scroll event, so returning the raw
 * offset would re-render the header for every pixel. A boolean
 * changes twice.
 */
export function useScrolledPast(threshold = 8): boolean {
  const read = useCallback(
    () => window.scrollY > threshold,
    [threshold],
  );
  return useSyncExternalStore(subscribeToScroll, read, () => false);
}

/* ----------------------------------------------------- in viewport ---- */

export { EMPTY as neverSubscribe };
