"use client";

import { useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { MAX_UPLOAD_BYTES_CLIENT, ACCEPTED_EXTENSIONS } from "@/lib/uploads";
import { cx } from "@/lib/cx";

/**
 * The upload target: a dashed orange panel that also takes a click.
 *
 * Both a drop target AND a real <input type="file">, because drag and
 * drop is unavailable to anyone using a keyboard or a touch screen.
 * The visible panel is a label for the input, so clicking it, tabbing
 * to it and pressing Enter all open the picker.
 */
export function Dropzone({
  onFiles,
  disabled,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handle(list: FileList | null) {
    if (!list || list.length === 0) return;
    onFiles(Array.from(list));
    // Reset so selecting the same file twice in a row still fires a
    // change event.
    if (inputRef.current) inputRef.current.value = "";
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        if (!disabled) handle(e.dataTransfer.files);
      }}
      className={cx(
        "relative rounded-card border-2 border-dashed p-10 text-center",
        "transition-all duration-200 ease-brand",
        dragging
          ? "border-ac bg-ac-tint"
          : "border-[color:var(--ac)]/30 bg-s1/40 hover:border-[color:var(--ac)]/55",
        disabled && "pointer-events-none opacity-50",
      )}
    >
      <input
        ref={inputRef}
        id="document-upload"
        type="file"
        multiple
        accept={ACCEPTED_EXTENSIONS.join(",")}
        onChange={(e) => handle(e.target.files)}
        // Visually hidden, not display:none -- a hidden input is not
        // focusable, so the keyboard path would disappear with it.
        className="sr-only"
      />

      <div
        aria-hidden="true"
        className="mx-auto flex h-12 w-12 animate-[bob_3.2s_var(--ease)_infinite] items-center justify-center rounded-card border border-[color:var(--ac)]/30 bg-ac-tint text-ac2 motion-reduce:animate-none"
      >
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 16V4" />
          <path d="M7.5 8.5 12 4l4.5 4.5" />
          <path d="M4 16v3.5h16V16" />
        </svg>
      </div>

      <p className="mt-4 text-[15px] font-medium text-tx">
        Drop documents here
      </p>
      <p className="mt-1 text-[13px] text-tx2">
        PDF, Markdown or plain text, up to{" "}
        {Math.round(MAX_UPLOAD_BYTES_CLIENT / (1024 * 1024))} MB each.
      </p>

      <div className="mt-5">
        <label htmlFor="document-upload">
          {/* A label styled as the button, so the real input stays the
              control. `asChild` would be neater but costs a dependency. */}
          <Button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={disabled}
          >
            Upload document
          </Button>
        </label>
      </div>

      <p className="mt-6 flex flex-wrap items-center justify-center gap-2 font-mono text-[11px] text-tx3">
        {["extract", "chunk", "embed", "ready"].map((stage, i) => (
          <span key={stage} className="inline-flex items-center gap-2">
            {i > 0 && <span aria-hidden="true" className="text-tx3">→</span>}
            {stage}
          </span>
        ))}
      </p>
    </div>
  );
}
