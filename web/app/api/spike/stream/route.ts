import { notFound } from "next/navigation";

/**
 * THROWAWAY. The U1 streaming spike.
 *
 * Proves one thing only: that an SSE response written by a Next route
 * handler reaches the browser PROGRESSIVELY rather than arriving as one
 * blob when the handler finishes. That is the single assumption the
 * whole chat screen rests on, and it is cheaper to falsify here than
 * after the chat UI is built on top of it.
 *
 * It emits ten events a quarter-second apart with no backend involved,
 * so a failure localises to the Next/platform layer rather than to
 * FastAPI, the LLM, or the proxy handler.
 *
 * Delete this route when U4 (Chat) is accepted.
 */

export const maxDuration = 300;
// No caching layer may sit between this and the browser, or the test
// measures the cache rather than the stream.
export const dynamic = "force-dynamic";

export async function GET() {
  // 404 in production: a public, unauthenticated endpoint that holds a
  // socket open for 2.5 seconds is a free amplification primitive, and
  // a diagnostic has no business shipping to users.
  if (process.env.NODE_ENV === "production") notFound();

  const encoder = new TextEncoder();
  const started = Date.now();

  const stream = new ReadableStream({
    async start(controller) {
      for (let i = 1; i <= 10; i += 1) {
        const payload = JSON.stringify({ i, elapsed_ms: Date.now() - started });
        controller.enqueue(encoder.encode(`event: tick\ndata: ${payload}\n\n`));
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      controller.enqueue(encoder.encode("event: done\ndata: {}\n\n"));
      controller.close();
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream",
      // no-transform is the load-bearing half: without it a proxy may
      // buffer or compress the stream and deliver it all at once, which
      // looks exactly like a broken stream.
      "cache-control": "no-cache, no-transform",
      "x-accel-buffering": "no",
      connection: "keep-alive",
    },
  });
}
