import { CSRF_HEADER, CSRF_VALUE } from "./csrf-shared";
import type { CitationsEvent } from "./types";

/**
 * Read an answer stream.
 *
 * `fetch` + a ReadableStream rather than `EventSource`, for three
 * independent reasons (ADR 0003):
 *   1. EventSource only issues GET. The question is a body, and
 *      putting user questions in a URL writes them into every log.
 *   2. It cannot set headers, so it cannot send the CSRF header.
 *   3. Its automatic reconnect is wrong here -- a dropped answer must
 *      not silently re-run, because that is another model call and the
 *      backend deliberately persists nothing on disconnect.
 */

export type StreamHandlers = {
  onToken: (text: string) => void;
  onCitations: (event: CitationsEvent) => void;
};

export class StreamAborted extends Error {
  constructor() {
    super("aborted");
    this.name = "StreamAborted";
  }
}

/**
 * Parse one SSE frame: a block of `field: value` lines.
 *
 * Returns null for a frame with no data line (a comment or keepalive).
 */
function parseFrame(block: string): { event: string; data: string } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // comment / keepalive
    if (line.startsWith("event:")) event = line.slice(6).trim();
    // The space after the colon is optional per the spec, and exactly
    // one leading space is stripped -- not trimmed, because a token
    // may legitimately begin with whitespace and trimming would eat
    // the spaces between words in a streamed answer.
    else if (line.startsWith("data:")) {
      const raw = line.slice(5);
      dataLines.push(raw.startsWith(" ") ? raw.slice(1) : raw);
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

export async function streamAnswer(
  conversationId: string,
  content: string,
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  // The fetch is wrapped because Stop can fire BEFORE the first byte
  // arrives -- while the server is still retrieving and reranking,
  // which is most of the wait. That rejection happens here, outside
  // the reader loop below, so without this it escaped as a raw
  // DOMException and the UI showed "signal is aborted without reason"
  // instead of "You stopped this answer." Caught by e2e test 7d.
  let response: Response;
  try {
    response = await fetch(
    `/api/conversations/${conversationId}/messages`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        [CSRF_HEADER]: CSRF_VALUE,
      },
      body: JSON.stringify({ content }),
      credentials: "same-origin",
      signal,
    },
    );
  } catch (caught) {
    if (signal.aborted) throw new StreamAborted();
    throw caught;
  }

  if (!response.ok || !response.body) {
    let message = "The answer could not be started.";
    let retryAfter: number | null = null;
    try {
      const body = (await response.json()) as { error?: string; detail?: string };
      message = body.error ?? body.detail ?? message;
    } catch {
      /* a non-JSON error body; the status is the only signal */
    }
    const header = response.headers.get("retry-after");
    if (header) {
      const seconds = Number(header);
      if (Number.isFinite(seconds)) retryAfter = Math.ceil(seconds);
    }
    const error = new Error(message) as Error & {
      status: number;
      retryAfterSeconds: number | null;
    };
    error.status = response.status;
    error.retryAfterSeconds = retryAfter;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // Frames are separated by a blank line, and a network chunk can end
  // ANYWHERE -- including halfway through a frame or even a UTF-8
  // character. `stream: true` on the decoder handles split characters;
  // this buffer handles split frames.
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);

        const frame = parseFrame(block);
        if (!frame) continue;

        if (frame.event === "token") {
          const { text } = JSON.parse(frame.data) as { text: string };
          handlers.onToken(text);
        } else if (frame.event === "citations") {
          handlers.onCitations(JSON.parse(frame.data) as CitationsEvent);
        }
        // `done` needs no handling: the loop ends when the body does.
      }
    }
  } catch (caught) {
    if (signal.aborted) throw new StreamAborted();
    throw caught;
  } finally {
    // Releasing the lock lets the connection be reused. On abort the
    // body is already cancelled, so this is a no-op there.
    reader.releaseLock();
  }
}
