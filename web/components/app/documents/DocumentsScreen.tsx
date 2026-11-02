"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useUser } from "@/components/app/UserContext";
import { Dropzone } from "@/components/app/documents/Dropzone";
import { Button } from "@/components/ui/Button";
import { StatusChip, StatusDot } from "@/components/ui/chips";
import { EmptyState, InlineBanner, ProgressBar, Skeleton } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";
import { ApiError, apiFetch, queryKeys } from "@/lib/api";
import { failureCopy } from "@/lib/site";
import { formatBytes, rejectReason } from "@/lib/uploads";
import type { Status } from "@/components/ui/chips";
import type { DocumentDetail, DocumentListItem, DocumentStatus } from "@/lib/types";

/**
 * The backend's lifecycle names, mapped to the chip's vocabulary.
 *
 * `pending` means "queued, not started" and the chip calls that
 * `queued` -- the user-facing word for a document waiting on a worker.
 * An explicit map rather than a cast, so adding a status to the
 * backend is a type error here instead of a chip rendering nothing.
 */
const CHIP_STATUS: Record<DocumentStatus, Status> = {
  pending: "queued",
  processing: "processing",
  ready: "ready",
  failed: "failed",
};

/** While anything is processing, re-ask this often. */
const POLL_MS = 3000;

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return new Date(iso).toLocaleDateString();
}

function FailureReason({ id }: { id: string }) {
  const user = useUser();
  // The list response has no error_message -- only the detail
  // endpoint does -- so a failed row fetches its own reason rather
  // than showing a generic "failed" the user cannot act on.
  const { data } = useQuery({
    queryKey: queryKeys.document(user.org_id, user.id, id),
    queryFn: () => apiFetch<DocumentDetail>(`documents/${id}`),
    staleTime: Infinity,
  });
  if (!data) return null;

  // An unrecognised failure still gets copy. Rendering nothing would
  // leave a red "Failed" chip with no explanation at all, which is the
  // state this whole change exists to remove.
  const copy = failureCopy[data.failure_code ?? "unknown"];

  return (
    <div className="mt-1.5 max-w-[52ch]">
      <p className="text-[12px] font-medium text-err-tx">{copy.title}</p>
      <p className="mt-0.5 text-[12px] leading-relaxed text-tx2">{copy.body}</p>
      <p className="mt-1 text-[12px] leading-relaxed text-tx3">{copy.action}</p>
    </div>
  );
}

export function DocumentsScreen() {
  const user = useUser();
  const isAdmin = user.role === "admin";
  const queryClient = useQueryClient();
  const toast = useToast();
  const [rejected, setRejected] = useState<string[]>([]);

  const documentsKey = queryKeys.documents(user.org_id, user.id);

  const { data, isPending, isError, error } = useQuery({
    queryKey: documentsKey,
    queryFn: () => apiFetch<DocumentListItem[]>("documents"),
    // Poll ONLY while something is actually in flight. A fixed
    // interval would keep a tab talking to the API forever on a page
    // where nothing can change.
    refetchInterval: (query) => {
      const rows = query.state.data as DocumentListItem[] | undefined;
      const busy = rows?.some(
        (d) => d.status === "pending" || d.status === "processing",
      );
      return busy ? POLL_MS : false;
    },
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const body = new FormData();
      body.append("file", file);
      // No content-type set: the browser must add its own multipart
      // boundary, and setting it by hand produces a body the server
      // cannot parse.
      return apiFetch<{ document_id: string }>("documents", {
        method: "POST",
        body,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: documentsKey });
    },
    onError: (caught) => {
      toast(
        "error",
        caught instanceof ApiError ? caught.message : "The upload failed.",
      );
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`documents/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: documentsKey });
      toast("info", "Document deleted.");
    },
    onError: (caught) => {
      toast(
        "error",
        caught instanceof ApiError ? caught.message : "Could not delete that.",
      );
    },
  });

  function onFiles(files: File[]) {
    const reasons: string[] = [];
    const accepted: File[] = [];
    for (const file of files) {
      const reason = rejectReason(file);
      if (reason) reasons.push(reason);
      else accepted.push(file);
    }
    // Rejected files are named with WHY, above the dropzone, rather
    // than vanishing silently or being reported one toast at a time.
    setRejected(reasons);
    accepted.forEach((file) => upload.mutate(file));
  }

  const rows = data ?? [];
  const busy = rows.some((d) => d.status === "pending" || d.status === "processing");

  return (
    <div className="mx-auto w-full max-w-[1100px] px-5 py-8 sm:px-10 sm:py-10">
      <div className="animate-fade-up flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-[30px] font-semibold tracking-display text-tx">
            Documents
          </h1>
          <p className="mt-1.5 text-[14px] text-tx2">
            {rows.length === 0
              ? "Nothing uploaded yet."
              : `${rows.length} document${rows.length === 1 ? "" : "s"} in this organization.`}
          </p>
        </div>
      </div>

      {!isAdmin && (
        <div className="animate-fade-up stagger-1 mt-6">
          <InlineBanner tone="info" title="You have read access">
            Uploading and deleting documents is limited to administrators.
          </InlineBanner>
        </div>
      )}

      {isAdmin && (
        <div className="animate-fade-up stagger-1 mt-6">
          <Dropzone onFiles={onFiles} disabled={upload.isPending} />
          {rejected.length > 0 && (
            <div className="mt-3">
              <InlineBanner tone="error" title="Some files were not uploaded">
                <ul className="list-disc space-y-1 pl-5">
                  {rejected.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </InlineBanner>
            </div>
          )}
        </div>
      )}

      <div className="animate-fade-up stagger-2 mt-8">
        {isError ? (
          <InlineBanner tone="error" title="Could not load documents">
            {error instanceof ApiError ? error.message : "Try again shortly."}
          </InlineBanner>
        ) : isPending ? (
          <div className="flex flex-col gap-2 rounded-card border border-line bg-s1 p-5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title="No documents yet">
            Upload a PDF, Markdown or text file and it will be indexed for
            questions. Processing happens in the background.
          </EmptyState>
        ) : (
          <div className="rounded-card border border-line bg-s1">
            {/* The table scrolls inside its own box; the page never
                scrolls sideways. */}
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-line">
                    {["Name", "Status", "Size", "Uploaded", ""].map((h) => (
                      <th
                        key={h}
                        scope="col"
                        className="px-5 py-3 text-[12px] font-medium uppercase tracking-wider text-tx3"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((doc) => {
                    const processing =
                      doc.status === "pending" || doc.status === "processing";
                    return (
                      <tr
                        key={doc.id}
                        className="border-b border-line last:border-0 transition-colors duration-200 hover:bg-s2/60"
                      >
                        <td className="max-w-[320px] px-5 py-4">
                          <p className="truncate text-[14px] font-medium text-tx">
                            {doc.filename}
                          </p>
                          {doc.status === "failed" && <FailureReason id={doc.id} />}
                        </td>
                        <td className="px-5 py-4">
                          <StatusChip status={CHIP_STATUS[doc.status]} />
                          {processing && (
                            <div className="mt-2 w-[150px]">
                              {/* Indeterminate: the backend reports a
                                  four-value status and no percentage,
                                  so a number here would be invented. */}
                              <ProgressBar label={`Processing ${doc.filename}`} />
                            </div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-5 py-4 font-mono text-[12.5px] text-tx2">
                          {formatBytes(doc.file_size_bytes)}
                        </td>
                        <td className="whitespace-nowrap px-5 py-4 text-[13px] text-tx2">
                          {relativeTime(doc.created_at)}
                        </td>
                        <td className="px-5 py-4 text-right">
                          {isAdmin && (
                            <Button
                              variant="danger"
                              className="h-9 px-3 text-[13px]"
                              disabled={remove.isPending}
                              onClick={() => remove.mutate(doc.id)}
                            >
                              Delete
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {busy && (
              <p
                // polite, so a screen reader hears "ready" when the
                // status settles instead of being interrupted on every
                // three-second poll.
                aria-live="polite"
                className="flex items-center gap-2 border-t border-line px-5 py-3 text-[12px] text-tx3"
              >
                <StatusDot tone="ok" />
                Checking status every {POLL_MS / 1000} seconds while documents
                are processing.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
