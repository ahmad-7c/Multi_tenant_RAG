# ADR 0003 — Upload size limit, and fetch streaming instead of EventSource

Status: accepted (U1, 2026-11-01)

Two small decisions forced by the same platform, recorded together.

---

## Part 1 — The upload limit is 4 MB in the UI, 25 MB in the backend

### Context

Verified against `vercel.com/docs/functions/limitations` (page last
updated 2026-08-24):

- **Request and response bodies are capped at 4.5 MB.** Over that, the
  platform returns `413 FUNCTION_PAYLOAD_TOO_LARGE` — a platform error
  page, not something the app can phrase.
- Max duration: Hobby 300s default and maximum; Pro/Enterprise 300s
  default, 800s maximum, 1800s extended (beta).
- **Streamed responses count against max duration**, since it covers
  "time spent processing the request and sending the response, including
  streamed responses".

The backend's own `MAX_UPLOAD_BYTES` is 25 MB, which is correct for a
self-hosted deployment with no such cap.

### Decision

`MAX_UPLOAD_BYTES = 4 * 1024 * 1024` in the frontend
(`web/lib/backend.ts`). The UI refuses a larger file with a sentence
before any bytes move.

The backend's 25 MB is **left alone**. It is the deployment-independent
limit; lowering it would break self-hosted installs to accommodate one
hosting provider.

### Consequences

- The effective limit depends on where the frontend runs, which is
  honest but needs saying: 4 MB on Vercel, 25 MB when the Next server is
  self-hosted next to FastAPI.
- A user on Vercel cannot upload a 10 MB PDF, and DocuMind is a document
  product, so this is a real functional limit, not a theoretical one.
- The production answer is a **presigned direct upload to object
  storage**, which bypasses the function body entirely. That needs the
  storage work already in the backlog (file access via signed URL) and is
  the same mechanism from the other direction.
- `maxDuration = 300` is set on the proxy route. The backend's
  `LLM_TOTAL_TIMEOUT_SECONDS` is 120s, so the budget that actually bites
  is the backend's — the right way round, since it is the one that can
  clean up after itself.

### Not yet enforced

The cap is a constant with no UI behind it, because uploads are U3. The
UI check and its test land there.

---

## Part 2 — SSE over `fetch`, not `EventSource`

### Context

Asking a question is `POST /conversations/{id}/messages` with a JSON body,
and the response is `text/event-stream`.

### Decision

Read the stream with `fetch` and a `ReadableStream` reader.

`EventSource` cannot be used, for three independent reasons:

1. **It only issues GET.** The question is a body, not a query parameter.
   Putting user questions in a URL would also write them into every
   access log.
2. It cannot set request headers, so it cannot send the CSRF header the
   BFF requires.
3. Its automatic reconnect is wrong here: a dropped answer stream must
   **not** silently re-run, because re-running costs another LLM call and
   the backend deliberately persists and caches nothing on disconnect.

### Consequences

- Reconnect, abort and parsing are ours to write. Abort is the feature,
  not a cost: passing the browser's `AbortSignal` through to the upstream
  fetch is what makes the Stop button actually cancel generation
  server-side.
- Frame parsing is hand-rolled, so a chunk boundary mid-frame has to be
  buffered rather than assumed. That lands with the chat client in U4.

### Verified

- A throwaway spike route (`/api/spike/stream`, 404s in production)
  delivered ten events ~250 ms apart rather than as one blob: measured
  inter-arrival times 137, 71, 266, 256, 259, 274, 256, 279, 234, 245 ms.
  So Next 16 streams progressively **locally**.
- Through the real proxy handler against the real backend, a question
  returned `event: token`, then `event: citations` carrying the full B1
  payload, then `event: done`, with `content-type: text/event-stream`,
  `cache-control: no-cache, no-transform` and `x-accel-buffering: no`.

### NOT verified

**Nothing has been tested on Vercel.** No preview deployment exists, so
every platform claim above comes from the documentation and not from
observed behaviour. Streaming through Vercel's edge, and whether an
answer can exceed the function duration under load, are both unmeasured.
