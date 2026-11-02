"use client";

import { useQuery } from "@tanstack/react-query";

import { useUser } from "@/components/app/UserContext";
import { apiFetch, queryKeys } from "@/lib/api";
import type { DocumentListItem } from "@/lib/types";

/**
 * A refusal, rendered as information rather than as a dead end.
 *
 * The backend's refusal sentence is correct but generic, because it
 * is a module constant the evaluation harness recognises by identity.
 * The part that is specific to THIS user -- which documents they can
 * actually ask about -- belongs here, where the document list is
 * already loaded and already org-scoped.
 *
 * Amber, not red. A refusal is the system working: it declined to
 * guess. Styling it as an error teaches people that careful behaviour
 * is a malfunction.
 */
export function RefusalCard({ text }: { text: string }) {
  const user = useUser();

  const { data } = useQuery({
    queryKey: queryKeys.documents(user.org_id, user.id),
    queryFn: () => apiFetch<DocumentListItem[]>("documents"),
    // Shared with the Documents screen, so this is usually a cache
    // read rather than a request.
    staleTime: 30_000,
  });

  const ready = (data ?? []).filter((d) => d.status === "ready");

  return (
    <div
      role="status"
      className="rounded-card border border-[color:var(--amber)]/25 bg-[color:var(--amber)]/[0.07] p-4"
    >
      <p className="text-[14px] font-semibold text-[color:var(--amber)]">
        I couldn&apos;t answer that from your documents
      </p>
      <p className="mt-1.5 text-[13.5px] leading-relaxed text-tx2">{text}</p>

      {ready.length > 0 ? (
        <div className="mt-3 border-t border-[color:var(--amber)]/20 pt-3">
          <p className="text-[12px] text-tx3">You can ask about:</p>
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {ready.slice(0, 6).map((doc) => (
              <li
                key={doc.id}
                className="rounded-pill border border-line bg-s2 px-2.5 py-1 font-mono text-[11.5px] text-tx2"
              >
                {doc.filename}
              </li>
            ))}
            {ready.length > 6 && (
              <li className="px-1 py-1 text-[11.5px] text-tx3">
                and {ready.length - 6} more
              </li>
            )}
          </ul>
        </div>
      ) : (
        // No documents is a DIFFERENT problem from a bad question, and
        // telling someone to rephrase when there is nothing to search
        // would send them round a loop they cannot exit.
        <div className="mt-3 border-t border-[color:var(--amber)]/20 pt-3">
          <p className="text-[12px] leading-relaxed text-tx3">
            No documents have finished processing yet, so there is nothing for
            me to search. Upload one on the Documents page.
          </p>
        </div>
      )}
    </div>
  );
}
