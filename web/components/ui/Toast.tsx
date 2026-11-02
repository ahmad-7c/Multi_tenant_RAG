"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

import { cx } from "@/lib/cx";

type Toast = { id: number; tone: "info" | "error"; message: string };

const ToastContext = createContext<((tone: Toast["tone"], message: string) => void) | null>(
  null,
);

export function useToast() {
  const show = useContext(ToastContext);
  if (!show) throw new Error("useToast must be used inside <ToastHost>");
  return show;
}

/**
 * Transient messages, announced to assistive tech.
 *
 * The live region is rendered ALWAYS, even when empty. A region added
 * to the DOM at the same moment its text appears is frequently not
 * announced -- the screen reader has to be watching the container
 * before the change happens.
 */
export function ToastHost({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const show = useCallback((tone: Toast["tone"], message: string) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { id, tone, message }]);
    // Errors stay longer: a user who missed one cannot re-read it.
    const ttl = tone === "error" ? 7000 : 4000;
    setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id));
    }, ttl);
  }, []);

  const value = useMemo(() => show, [show]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        // polite, not assertive: a toast interrupting mid-sentence is
        // worse than it arriving a moment later.
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-6 right-6 z-50 flex w-[min(360px,calc(100vw-3rem))] flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={cx(
              "animate-fade-up pointer-events-auto rounded-card border px-4 py-3 text-[13px]",
              toast.tone === "error"
                ? "border-[color:var(--err)]/35 bg-s2 text-err-tx"
                : "border-line2 bg-s2 text-tx",
            )}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
