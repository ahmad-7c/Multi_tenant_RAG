# DocuMind

Multi-tenant RAG-as-a-service. Organisations upload documents, their
people ask questions, and answers come back cited and grounded in
retrieved content only — never from the model's training data.

FastAPI + PostgreSQL/pgvector + Redis + Celery behind a Next.js BFF.
Deployment is documented step by step in
[`deploy/runbook.md`](deploy/runbook.md); the decisions behind it are in
[`docs/adr/`](docs/adr/).

---

## Known limitations

Written down because a gap nobody has recorded gets rediscovered by a
user. Nothing here is a surprise; all of it is deliberate scope.

### Chat and conversations

- **No conversation switcher.** The most recent conversation is adopted
  on load and "New conversation" starts another, but there is no UI to
  move between them. The API supports listing; the screen does not use
  it.
- **`[1]` markers render as plain text on a reloaded conversation.** A
  live answer resolves its markers into citation chips from the
  `citations` SSE event. A persisted turn stores only
  `cited_chunk_ids` — not the marker-to-source mapping — so after a
  refresh the same answer shows bare `[1]` instead of a chip. Honest,
  and visibly inconsistent with how the turn looked a minute earlier.
- **A failed or empty document is invisible in chat.** Asking about a
  document whose ingestion failed returns an ordinary "nothing found"
  refusal, with no hint that the document carries no indexed text. This
  was the original confusion that prompted the conversational-assist
  work and it is still not addressed.
- **Ingestion gives up on a provider 429 instead of backing off.** A
  document that hits Gemini's free-tier limit mid-ingestion is
  permanently failed and must be deleted and re-uploaded. The failure
  message says so; the retry that would avoid it is not built.

### Usage and limits

- **No budget meter**, in the sidebar or on the Usage screen. The daily
  budget counters exist in Redis, but `check_budget` only ever raises or
  returns `None`, so nothing can read the remaining amount. The endpoint
  is the missing piece, not the UI.
- **"Where the time goes" is a single average-latency card.** The
  three-bar per-stage breakdown the design called for would need
  per-stage timings aggregated over a window; the backend measures them
  per request only. One real number instead of three invented ones.

### Documents

- **No "Open in document".** It needs a short-lived signed URL from an
  authenticated, org-scoped endpoint, which does not exist. A link that
  404s is worse than no link.
- **No Pages column, no per-stage upload progress, no chunk count in
  the list.** `Document` has no `page_count` (and `.txt`/`.md` have no
  page concept — ingestion deliberately does not fake one),
  `DocumentStatus` is a four-value enum with no percentage, and
  `chunk_count` is on the detail response but not the list one. The
  progress bar is indeterminate by design rather than by omission.
- **The 4 MB upload cap has two layers and one gap.** The dropzone
  refuses an oversized file before any network call, and the BFF now
  refuses one by `content-length`. A **chunked** upload sends no
  `content-length`, so that request falls through to the backend's own
  25 MB cap. Checking it would mean buffering the stream, which is what
  the streaming proxy exists to avoid.

### Verification

- **Lighthouse has never been run.** axe reports zero serious or
  critical findings across ten screens, which is a different and weaker
  claim than the "accessibility ≥ 95" in the definition of done.
- **Responsive widths are unmeasured.** No horizontal-scroll assertion
  exists at 1440, 1024 or 390.
- **The end-to-end suite runs against fake providers**, so a change in a
  provider's response shape is not caught there — only by a deliberate
  live smoke.
- **The client-IP attestation is not verified live.** The code and tests
  are in; nothing has run against the real Vercel deployment. See
  `deploy/runbook.md` §12.7.

### Operations

- **No off-box backup yet.** §11 produces a nightly dump, retains seven
  days and drills the restore, but the copy lives on the same disk as
  the database. §11.6 costs the options; the decision is open.
- **No point-in-time recovery.** A nightly dump means up to 24 hours of
  loss.
- **Nothing alerts on a failed backup.** The cron job writes to a log
  nobody reads.
- **The reranker breaker clears only on process restart.** Correct for a
  configuration mistake, wrong for a provider-side 401 that later
  resolves on its own.
- **The in-memory rate-limit fallback is per process.** With N API
  processes an org gets N buckets. It bounds the worst case rather than
  being a correct distributed limiter, and it only applies while Redis
  is unreachable.

### Evaluation

- **`eval/calibrate.py` has no unit tests.** It is driven only by live
  runs and reviewed by hand against the run file, so a mutation in it
  would survive. It is the largest untested module in the tree.
- **Judge agreement is n=3.** Three out of three after a prompt fix,
  which is not enough to conclude anything; `meets_bar` returns `None`
  below ten grades. The shortcut that makes the judge cheap is what
  makes it hard to measure.
- **Retrieval at scale is unmeasured.** recall@8 = 1.000 over 40 child
  chunks per tenant — the top 8 is a fifth of that corpus, so the figure
  proves wiring, not scale.
- **Answer completeness over a parent chunk is unproven.** Two of three
  `spans_two_chunks` cases answered half the span while the parent sent
  to the model contained both halves.
