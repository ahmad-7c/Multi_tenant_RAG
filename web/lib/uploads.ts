/**
 * Upload constraints, shared by the dropzone and the mutation.
 *
 * Client-safe (no `server-only`), because the UI must refuse an
 * oversized file BEFORE sending it -- the point is to give the user a
 * sentence instead of a platform error page after a slow upload.
 *
 * The number is re-declared here rather than imported from
 * lib/backend.ts because that module is server-only; importing it
 * into a client component would be a build error. Kept in sync by the
 * comment on both sides and by one test in U6.
 */

/** Vercel caps function request bodies at 4.5 MB (413 above it). */
export const MAX_UPLOAD_BYTES_CLIENT = 4 * 1024 * 1024;

/** What the backend's extractor can actually read. */
export const ACCEPTED_EXTENSIONS = [".pdf", ".md", ".txt"] as const;

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Returns a reason to reject, or null to accept. */
export function rejectReason(file: File): string | null {
  const dot = file.name.lastIndexOf(".");
  const ext = dot === -1 ? "" : file.name.slice(dot).toLowerCase();
  if (!ACCEPTED_EXTENSIONS.includes(ext as (typeof ACCEPTED_EXTENSIONS)[number])) {
    return `${file.name}: only ${ACCEPTED_EXTENSIONS.join(", ")} files can be read.`;
  }
  if (file.size > MAX_UPLOAD_BYTES_CLIENT) {
    return `${file.name} is ${formatBytes(file.size)}. The limit is ${formatBytes(MAX_UPLOAD_BYTES_CLIENT)}.`;
  }
  if (file.size === 0) {
    return `${file.name} is empty.`;
  }
  return null;
}
