# DocuMind — Project Intelligence File

## What This System Is
Multi-tenant RAG-as-a-service platform. Companies upload documents,
employees ask questions, answers come back cited and grounded in
real retrieved content only.

## Non-Negotiables (Never Violate These)
- Every tenant-owned table MUST have org_id (non-nullable FK to organizations)
- org_id on JWT payload only — never trust org_id from request body/params
- Every DB query on tenant data MUST filter by org_id
- Passwords: bcrypt hash only, never stored or logged plain
- API responses: never expose hashed_password field
- Document ingestion: async via Celery only, never synchronous in request

## Architecture Decisions Already Made (Can review and edit accordinging to instructions)
- Shared DB, shared schema, discriminator column (org_id) for multi-tenancy
- UUID PKs (not integer) — prevents enumeration attacks
- Composite unique (org_id, email) on users — same email can exist across orgs
- Parent-child chunking — child chunks embedded+searched, parent chunks sent to LLM
- pgvector over Qdrant for v1 — simpler ops, same Postgres instance
- bcrypt==4.0.1 pinned — 4.1+ breaks passlib 1.7.4

## Tech Stack
FastAPI, PostgreSQL + pgvector, SQLAlchemy 2.0, Alembic,
Redis, Celery, PyMuPDF (PDF extraction), OpenAI embeddings,
PyJWT, passlib, Docker Compose, pytest

## Code Patterns To Follow
- Route functions stay thin: accept request, call service, return response
- Business logic lives in app/services/, never in routes
- Pydantic schemas: separate Create/Response schemas per entity
- DB session: always via get_db() dependency, never created manually
- Auth: always via get_current_user() dependency

## How To Comment Code
Every non-obvious decision must have a comment explaining WHY,
not what. The what is visible in the code. The why is not.
Example: "# denormalized here to avoid join on hot retrieval path"
not "# stores the org id"

## Before Implementing Anything Significant
1. State the approach you're taking
2. State the alternative you're NOT taking and why
3. Then write the code

## Current Phase
**PHASES 1-3 COMPLETE. Free-provider migration COMPLETE. PHASE 4
COMPLETE: golden set of 40 (38 gate-scored), retrieval and generation
scored separately, thresholds re-calibrated at n=38 from a live run and
applied, rate limiting + stream slots + budget guard, usage metering,
and 12 mutations proving the tests bite.** Dev DB at migration 0008.

**UI PHASE COMPLETE: Step 0, B1, B1b, U1 through U6.** 833 backend
tests green against real Postgres + pgvector with no network
(the suite is pinned offline by construction). The frontend has NO
tests yet -- Playwright and axe are U6.

`.env` is LIVE: `EMBEDDING_PROVIDER=gemini`, `LLM_PROVIDER=gemini`,
`RERANKER_PROVIDER=hosted`. Backup at `.env.backup-pre-live`. The test
suite is unaffected either way -- see the offline guard in
`tests/conftest.py`.

**Next: a Vercel preview**, which is the only thing in the definition
of done that no local run can establish. Lighthouse and the three
responsive widths are also still unmeasured -- see U6's NOT verified
list.

### UI phase decisions, locked at C0 (2026-10-31)
Skills: `documind-ui-phase` (how: architecture, security, tests) and
`documind-ui-design` (what: visuals). On conflict, security and
architecture follow the phase skill, visuals follow the design skill.
NB the design skill's folder on disk is still `documin-ui-design`
(missing the `d`); the skill name inside it is correct.

- **App shape: Next.js BFF, same origin.** The browser talks only to
  Next.js; Next.js server code talks to FastAPI. **CORS is deliberately
  NOT added to the backend** -- there is no `add_middleware` call in
  `app/main.py` and that is the secure default, not an oversight.
- **Auth unchanged on the backend**: OAuth2 bearer, header only. The BFF
  holds the JWT in an httpOnly cookie and attaches
  `Authorization: Bearer` on each proxied call. The browser never sees
  the token.
- **Omitted because the backend does not provide them** (each is in the
  backlog below): Documents 'Pages' column, stage-by-stage processing
  progress (use an indeterminate bar plus the status chip), 'Open in
  document', and the sidebar + Usage budget meters.
- **'Where the time goes' is replaced by an Average latency card**, from
  the per-day `avg_latency_ms` that `GET /api/v1/usage` already returns.
  The three-bar breakdown it replaced would have needed per-stage
  timings aggregated over a window; B1 added them per REQUEST only.
- **The `/health` chip shows the overall status word only.** The names in
  `degraded[]` are operator-level deployment facts and are never
  rendered to a user.
- Cap list sizes in the UI; no pagination work this phase. Dark theme
  only.

### B1: honest backend data for the UI (2026-10-31)
The UI's trace chips, relevance bars and source passages are now real
fields. Before B1 none of them reached a client: the gate score was
computed and discarded (`should_answer` returned a bare bool), no
per-stage timing was measured anywhere, and `Citation` had no score and
no passage. **18 new tests in `tests/test_trace_payload.py`.**

- **The `citations` SSE event gained** `confidence`, `scores_calibrated`,
  `used_reranker_fallback`, `candidate_count`, `parent_count`,
  `retrieve_ms`, `rerank_ms`, `first_token_ms`. Existing fields
  (`answer`, `citations`, `cached`, `gated`, `rewritten_question`) keep
  their meaning, pinned by a test -- the BFF will read these and a
  silent change of meaning would error nowhere.
- **Each citation gained** `passage` (the parent text, capped at
  `PASSAGE_CHARS = 1200`, cut at a sentence boundary), `matched_chunk_id`
  (the child that retrieved, so the UI can highlight the matched
  sentence) and `score`.
- **Absent is null, never 0.** `candidate_count`/`parent_count` changed
  from `int = 0` to `int | None = None`. On a cache hit phase 1 returns
  before measuring anything, so counts and timings stay None; the design
  skill's rule is to hide a null element, and `candidates 0` /
  `rerank 0 ms` are false statements about the request rather than
  absent measurements. Same reasoning as the nullable token columns on
  `usage_events`. NB `parent_count` IS 0 when parents were genuinely
  mapped and none came back -- a real measurement of zero, deliberately
  distinguishable from the gate having refused first.
- **`passage` comes from `ContextBlock.content`, already in memory.**
  Alternative NOT taken: a new DB fetch of parent text at citation time.
  Rejected because it would (a) add a SECOND place the tenant filter has
  to be right, for no gain, and (b) reopen a session after phase 1 --
  the one thing query_service's three-phase split exists to prevent.
  `ContextBlock` gained `score` and `matched_chunk_id`, both defaulted,
  carried from `RetrievedParent`.
- **Passages are attached inside validate_citations' cited-markers
  loop**, which makes "only cited sources" structural rather than a
  filter someone has to remember. Uncited blocks are the model's
  rejected context: the user never saw them, and shipping them would
  quietly widen what one question discloses.
- **One predicate decides whether a score is relevance.**
  `reranker.scores_are_relevance(reranker, used_fallback)` =
  `scores_are_calibrated and not used_fallback`, and `should_answer` now
  branches on the same call. Both conditions matter and the second is
  the subtle one: `scores_are_calibrated` is a property of the PROVIDER,
  so a hosted cross-encoder advertises calibrated scores and still
  returns RRF order when its call fails -- the provider flag alone is
  True at exactly the moment the scores are rank reciprocals. An RRF
  score rendered in a 0..1 bar reads as "almost no match" for every
  source of every answer.
- **The score is nulled TWICE**, where the Citation is built
  (`expose_scores`, default False) and where the payload is serialised
  (`expose_score`). The second catches a score replayed from the cache,
  written under a reranker configuration that can no longer be verified.
  Because the two are redundant, **each is pinned by its own unit test**
  -- a single-site mutation is otherwise invisible and the defence is
  one layer deep without anyone knowing which layer.
- **Backward compatibility was a real hazard.** The cache read path is
  `Citation(**cached_dict)`, so a required new field would have raised
  TypeError on every key written before B1 -- a deploy-time outage
  lasting one TTL. All three new fields default to None, pinned by
  `test_citations_cached_before_b1_still_load`.
- `first_token_ms` is time-to-first-token of the MODEL call, measured in
  `generate_answer`. Null on a cache hit and a gated refusal (no model
  call) and null when the stream FAILED: partial text is already
  discarded on failure, so a timing for it would describe an answer
  nobody received.
- A gated refusal keeps real `retrieve_ms`, `rerank_ms`,
  `candidate_count` and `confidence`. On a refusal the gate score is the
  most informative number available, because it says how close the best
  match came.

#### B1 mutation checks: 5 applied one at a time, 5 caught
`scripts/mutate.py` mutations 13-17. Each was confirmed to make a NAMED
test fail, then reverted.

| mutation | caught by |
|---|---|
| `b1-expose-score-under-fallback` | `test_no_scores_under_reranker_fallback_...` |
| `b1-validate-always-exposes-scores` | `test_validate_citations_withholds_the_score_unless_asked` |
| `b1-payload-ignores-score-gate` | `test_the_citation_payload_withholds_the_score_unless_exposed` |
| `b1-passages-for-uncited-sources` | `test_passage_is_sent_only_for_cited_sources` |
| `b1-passage-fetch-drops-org-filter` | `test_reranker.py::test_parent_mapping_is_org_scoped` |

#### B1b: every tenant filter on the passage path is now individually pinned (2026-11-01)
B1's end-to-end isolation test was measured rather than trusted, and on
its own it proved almost nothing. There are FOUR tenant filters between
org B's question and a passage -- the vector leg, the keyword leg,
retrieval's hydration fetch, and `map_to_parents` -- and they are
mutually redundant. Removing any ONE left the whole suite green,
including `test_retrieval_never_returns_another_orgs_chunk`, because
whichever filters remained covered for the one that went. **A filter
nothing pins is a filter a refactor can delete silently.**

Three new tests in `test_retrieval.py` each execute THAT LAYER'S OWN
statement (`_VECTOR_SQL`, `_TEXT_SQL`, `_HYDRATE_SQL`) against another
org's data, so no other layer runs and none can pass the test on its
behalf. Hydration is handed a foreign chunk id explicitly -- the input a
broken leg would produce.

Mutations 18-20 in `scripts/mutate.py`, each applied alone and reverted:

| filter removed | vec test | kw test | hyd test | old e2e | B1 e2e |
|---|---|---|---|---|---|
| vector leg | **FAIL** | pass | pass | pass | pass |
| keyword leg | pass | **FAIL** | pass | pass | pass |
| hydration fetch | pass | pass | **FAIL** | pass | pass |

A clean diagonal: one test per filter, no overlap, nothing else
disturbed. `map_to_parents` was already pinned this way by
`test_reranker.py::test_parent_mapping_is_org_scoped`.

**NO LEAK WAS FOUND.** Every filter was present and correct; the entire
gap was in what the suite pinned. The B1 end-to-end test is kept as a
backstop and its docstring now names the four per-layer tests, so it
cannot be mistaken for covering them. Same lesson as Phase 4's
`leak-slot-on-disconnect`, found a second time by looking: **a test named
for a failure mode is not evidence that it exercises that failure mode.**

#### Fixed: the passage window excluded the matched chunk (2026-11-01)
A real bug in B1 as first written, found by asking the question rather
than assuming. `_passage` returned the parent's FIRST 1200 characters.
A parent runs to ~500 tokens, well over that, so for any match in the
back half the reader was shown the wrong part of the right page: the
`matched_chunk_id` highlight resolved to nothing and the excerpt visibly
failed to contain the answer printed above it. **The reader's conclusion
would be that the citation is wrong** -- the exact opposite of what the
passage exists to establish. Confirmed with a test that places the
matched child past the cap, which failed against the original code.

- `_window_start` now anchors the window on the match when the match
  falls past the cap, placing it about a third of the way in so there is
  leading context, and snapping to a preceding sentence boundary.
- A moved window is prefixed `...` so the reader knows text precedes it.
- The returned string INCLUDING its ellipses stays within
  `PASSAGE_CHARS`, because the cap is what the UI lays out against.
- A child's text does not always appear verbatim in its parent (children
  are overlapping token windows and may split mid-sentence), so the
  lookup falls back to the child's first 120 characters, then to a
  start-anchored window. Never guesses.
- This needed the matched child's TEXT, not just its id:
  `RetrievedParent.matched_content` and `ContextBlock.matched_content`,
  carried from `Candidate.content`, which the search query already
  fetched. Never rendered into the prompt -- the model has the whole
  parent already.

#### Cached passages and document deletion (2026-11-01)
Cache invalidation became a DISCLOSURE boundary, not just a freshness
one: a cache entry now holds up to 1200 characters of document text per
citation, so "the corpus changed" and "this text may no longer be shown"
are the same question. `delete_document` already bumped
`index_version` in the same transaction as the delete, and the cache key
hashes org_id + question + index_version -- so the key a cached passage
lives under can no longer be COMPUTED by any later request. The entry is
unreachable, not merely stale, which is the property that matters when
the payload is document text. Pinned end to end by
`test_deleting_a_document_makes_its_cached_passage_unreachable`
(`test_documents.py::test_delete_increments_index_version` pins the bump
itself).

#### The `citations` event has a published contract (2026-11-01)
`CitationsEvent` / `CitationPayload` in `app/schemas/conversation.py`,
wired to the SSE route via `responses=`, **never `response_model`** -- a
response_model would make FastAPI validate and serialise a return value,
which for a StreamingResponse means buffering the whole answer and
losing the stream. Documentation only, so it can drift into fiction:
`test_the_openapi_schema_matches_what_the_route_actually_emits` asserts
the documented field names equal the emitted keys, at both levels.

`first_token_ms` is documented as **MODEL time-to-first-token** -- from
opening the provider stream to its first text chunk -- and the
description says in as many words that it is NOT user-perceived time to
first text, because it excludes auth, rate limiting, retrieval and
rerank (all reported separately on the same event) plus all network time
to the browser. The UI labels it "model first token" and measures
perceived latency client-side. A test pins that wording, since a chip
sitting next to two other timings is easy to mislabel.

Known wart: FastAPI advertises `application/json` alongside
`text/event-stream` for the 200 response even with
`response_class=StreamingResponse`. Cosmetic, and not worth hand-writing
`openapi_extra` (which shallow-replaces the whole `responses` object) to
remove.

#### B1 verified live, once (2026-11-01)
One real question against the dev corpus (`Acme-0d2624ff`,
`travel-policy.md`), through the real pipeline with live Gemini + live
Cohere: one embedding call, one rerank, one LLM call. Zero DB writes --
`persist_turn` was not called. The full payload:

    answer         "Expense reports must be submitted within thirty days
                    of the end of travel [1]."
    confidence     0.8668671      scores_calibrated  true
    score          0.8668671      used_fallback      false
    candidate_count 5             parent_count       5
    retrieve_ms    289            rerank_ms          3001
    first_token_ms 894            cached/gated       false/false
    passage        the full parent (219 chars, under both caps, so
                   snippet == passage -- correct, not a bug)

- Every new field carried a real measured value, and `confidence`
  equalled the cited source's `score` as it should when there is one
  parent at rank 1.
- **`rerank_ms` was 3001 ms against `RERANKER_TIMEOUT_SECONDS = 3.0`.**
  The call SUCCEEDED (`used_reranker_fallback` false, and 0.8669 is a
  real cross-encoder score -- RRF tops out near 0.016), so this is not a
  fallback. But it finished within a millisecond of the budget, meaning
  this deployment is one hair from degrading to RRF order on every query,
  with the confidence gate opening as a consequence. Likely a cold TLS
  handshake plus a cold Cohere model on the session's first call, which
  is exactly the condition a user's first question hits. **This is the
  first time that was visible at all** -- it is precisely the signal B1's
  timings were added to surface, found on the first live call. Not
  changed here: raising the budget is a latency/quality tradeoff and an
  operator decision.

#### Not touched by B1
Nothing parses the `citations` event in `eval/` or `scripts/`, and the
SSE route has no `response_model`, so there were no OpenAPI examples or
harness readers to update. Eval results are unchanged: `expose_scores`
defaults False and `eval/calibrate.py` carries its own
`candidate_count` on its own dataclass.

### U1: scaffold, tokens, components, BFF, streaming spike (2026-11-01)
Frontend lives in `web/` -- the repo root already has `app/` (the Python
package), so a Next project at root would collide. Next 16.4.3, React
19.3, Tailwind 4.3.3, TypeScript 5.9.3. Every dependency pinned EXACTLY
(no `^`). `npm audit`: 0 critical, 0 high, 16 moderate -- all in the
eslint toolchain, all dev-only, all rooted at one advisory for
`minimatch`. Nothing moderate reaches the browser bundle.

#### Three things that were wrong and are worth remembering
- **Tailwind never ran, and the build went GREEN anyway.**
  `create-next-app` installed `@tailwindcss/turbopack` but did not add
  the `turbopack.rules` entry that activates it, so `next build` emitted
  the raw stylesheet with one warning -- `Unknown at rule: @theme` --
  and ZERO utility classes. Every `bg-s1` and `rounded-card` in the app
  resolved to nothing. Caught by grepping the built CSS for
  `box-sizing` and `bg-s1` rather than by trusting the build summary:
  14 KB of CSS with no preflight and no `--tw-` anywhere. After the fix,
  33 KB with every token utility present. **A green build is not
  evidence that the CSS pipeline ran.**
- **Every page was statically rendered, which a nonce CSP silently
  breaks.** Next injects the CSP nonce during server-side rendering by
  reading the header off the incoming request; a prerendered page was
  built when no request existed, so it ships script tags with no nonce
  and the CSP blocks them. Blank screen in production, nothing wrong in
  development. Fixed with `force-dynamic` in the root layout; the build
  now reports every route as dynamic. Costs nothing real -- every screen
  behind /login is per-user and per-tenant and was never cacheable.
- **The BFF smoke test passed against the WRONG BACKEND.** `/api/health`
  returned 200 and `{"status":"healthy"}` -- from *Memoir App API*,
  another project holding host port 8000 (a collision CLAUDE.md already
  documented). DocuMind returns `{"status":"ok"}`. Every proxy assertion
  up to that point was meaningless. DocuMind now runs on **8001** for UI
  work and `web/.env.local.example` says so. **A 200 from a proxy proves
  something answered, not that the right thing answered.**

#### Next 16 renamed middleware
`middleware.ts` is deprecated in Next 16.0.0 and renamed to `proxy.ts`
with the function renamed to match (`export function proxy`). It defaults
to the Node.js runtime. The phase skill says "middleware.ts"; that
wording predates Next 16. A codemod exists
(`npx @next/codemod middleware-to-proxy`).

#### The BFF
- `app/api/[...path]/route.ts` -- the only path from browser to FastAPI.
  Reads the httpOnly cookie server-side and mints
  `Authorization: Bearer`. The browser never holds the JWT.
- **The allowlist excludes `auth` on purpose.** The backend's
  `/auth/login` responds `{access_token}`, so a generic passthrough
  would hand the token to the browser and undo the whole design in one
  word. Login, register and logout have their own handlers. Verified:
  `GET /api/auth/login` is 405 (the dedicated handler wins over the
  catch-all), not a proxied 200.
- Login is TWO upstream calls and register is THREE, because the backend
  returns a token but no profile from login, and no token at all from
  register. The body the browser gets is always the profile.
- `cookie` and `authorization` are in the hop-by-hop strip list.
  Forwarding the browser's cookie jar to FastAPI would send the session
  cookie to a service that authenticates by header -- ignored at best,
  logged upstream at worst.
- `signal: request.signal` is passed to the upstream fetch. That one line
  is what will make Stop work: a browser abort aborts the upstream
  request, the backend sees the disconnect and stops paying for tokens.
- Redis-style asymmetry is preserved: upstream 401 CLEARS the cookie, so
  a dead token does not loop through requests that will 401 again.
- CSRF is two independent checks: a required custom header (a
  cross-origin form cannot set one, and setting it from JS forces a CORS
  preflight this app never answers) plus an Origin-vs-Host comparison.
  Compared against the request's own Host rather than a configured site
  URL, so it keeps working on every Vercel preview with no per-
  environment variable to forget. The header's value is not a secret --
  its PRESENCE is the signal.
- Security headers in `next.config.ts` (nosniff, Referrer-Policy,
  Permissions-Policy, X-Frame-Options, no X-Powered-By); the CSP is in
  `proxy.ts` because it carries a per-request nonce.
- `server-only` on `lib/backend.ts`, `lib/session.ts`, `lib/csrf.ts`, so
  an accidental client import is a BUILD error rather than a bundle that
  ships `BACKEND_URL`. The CSRF constants are split into
  `lib/csrf-shared.ts` because the client legitimately needs the header
  name while the check itself must not ship.
- `next.config.ts` has no `eslint` key: Next 16 removed it from
  `NextConfig` along with `next lint`.

#### Design system (D0)
`app/tokens.css` is the only place a colour, radius or easing is
defined; `globals.css` maps them into the Tailwind 4 theme with
`@theme inline` so utilities emit `var(--ac)` rather than copying hex,
which is what will let a light theme be added by overriding tokens
alone. Fonts are Sora / Inter / JetBrains Mono via `next/font/google`,
self-hosted at build -- which is why `font-src 'self'` needs no external
host and first paint makes no third-party request. Only the weights the
spec names are loaded.

Components: Button (primary/ghost/danger + IconButton), Input,
StatusChip, CitationChip, SourcePill, TraceChip, StatusDot, Avatar,
Card, NavItem, ProgressBar, Segmented, Sparkline, EmptyState,
InlineBanner, Toast, Skeleton. `/design` renders every one in every
state and **404s in production** via a runtime check, not a build flag:
a flag that must be set correctly at deploy time will one day be wrong,
and that page renders sample numbers.

- **`TraceChip` cannot accept a null value.** `value: string`, required.
  The caller must omit the chip when the backend sent null rather than
  the component rendering a dash or a zero. The data-honesty rule made
  structural instead of remembered.
- **`ProgressBar`'s `value` is optional, and undefined means
  indeterminate.** `DocumentStatus` has no percentage, so a processing
  document genuinely has no number and a fake 60% would be invented.
- **`BudgetMeter` and `Ring` were NOT built**, per C0: the daily budget
  has no endpoint. `/design` lists them under "Not built" with the
  reason, so the gap is visible to the fidelity reviewer rather than
  looking like an oversight.
- Everything is a real `<button>`/`<a>`: `NavItem` is a `Link` with
  `aria-current`, `Segmented` is a `radiogroup`, `Input` has a real
  `<label for>` (a placeholder is not a label), icon buttons are 44px on
  touch and labelled. `StatusChip` is always icon AND text -- colour
  alone is exactly what a red/green deficiency loses.

#### Streaming spike: PASSES locally, UNVERIFIED on Vercel
`/api/spike/stream` (throwaway, 404s in production) delivered ten events
~250 ms apart -- inter-arrival 137, 71, 266, 256, 259, 274, 256, 279,
234, 245 ms -- so Next 16 does not buffer SSE locally. Then the same
confirmed through the REAL proxy against the REAL backend: `token`,
`citations` with the full B1 payload, `done`, with
`content-type: text/event-stream`, `cache-control: no-cache, no-transform`
and `x-accel-buffering: no`. **No Vercel preview exists, so every
platform claim is from documentation, not observation.**

Verified against vercel.com/docs/functions/limitations (updated
2026-08-24): request AND response bodies cap at **4.5 MB** (413
FUNCTION_PAYLOAD_TOO_LARGE); max duration Hobby 300s, Pro 800s
(1800s beta); **streamed responses count against max duration**. The UI
cap is 4 MB; the backend's 25 MB is deliberately unchanged because it is
correct for a self-hosted deployment. See ADR 0003.

#### A B1 defect the live U1 run found
`scores_calibrated` was reporting **true on an empty corpus**, where
nothing was scored at all. `scores_are_relevance` can only see the
reranker's config and whether the call fell back; neither knows that the
ranking came back empty. Nothing false was displayed -- confidence was
null and there were no citations, so the UI had nothing to render --
which is precisely why it needed a test rather than a bug report: the
flag contradicted its own documented meaning while every visible value
stayed honest. It also disagreed with the cache-hit path, which reports
false for the same reason. Now `bool(ranked) and
scores_are_relevance(...)`, pinned by
`test_scores_are_not_called_calibrated_when_nothing_was_scored` and
re-confirmed live. **735 tests green.**

Noted, not changed: a gated question with zero candidates reports
`rerank_ms: 0`, which is a true measurement of a step that had nothing to
send. The UI should suppress trace chips entirely when
`candidate_count` is 0 rather than render "rerank 0 ms".

#### ADRs written
`docs/adr/0001-bff-with-httponly-cookie.md`,
`0002-nonce-csp-forces-dynamic-rendering.md`,
`0003-upload-size-limit-and-fetch-streaming.md`. Markdown sanitisation
and the honest-data rules get theirs when the code exists (U4/U5).

#### Local dev for the UI -- PORTS
**Backend 8010, frontend 3010.** Host 8000 is held by Docker and
another project answers there, and 3000 collides with everything else;
both were moved so neither can silently hit the wrong service.

    docker compose up -d db test-db redis
    .env\Scripts\python.exe -m uvicorn app.main:app --port 8010 --reload
    cd web ; npm run dev                  # http://localhost:3010/design

One number per place, all agreeing: `web/.env.local`
(`BACKEND_URL=http://127.0.0.1:8010`), `web/.env.local.example`, the
fallback in `web/lib/backend.ts`, and `-p 3010` in both the `dev` and
`start` scripts in `web/package.json`. **A wrong BACKEND_URL does not
fail loudly -- it proxies to whatever answers**, which is how the first
BFF smoke test passed against another project's API.

Placeholder pages exist at `/login` and `/chat` so the proxy's redirect
target is not a 404 during review; both are replaced in U2/U4. The real
thing to look at today is `/design`.

### U2: public site, auth screens, CORS (2026-11-01)
**749 backend tests green.** Frontend lint and typecheck clean, build
clean. New pages: `/` (landing), `/how-it-works`, `/how-its-built`,
`/login`, `/register`. Sign-in verified end to end through the real
form: cookie set, redirect to `/chat`.

#### CORS -- added on request, and it REVERSES a C0 decision
C0 locked "BFF, same-origin, do NOT add CORS", and the phase skill
locks it too. The operator asked for it directly, so it exists, built
so the locked decision still holds by default:

- `CORS_ALLOW_ORIGINS`, comma-separated, **empty by default**. Empty
  adds NO middleware at all rather than a permissive default.
- `allow_credentials=True` with explicit origins. A wildcard **raises
  at startup** -- `*` cannot be combined with credentials, so browsers
  reject it outright and configuring it would break every call rather
  than loosen anything. Failing with the word "wildcard" in the message
  beats an afternoon of opaque CORS errors.
- `expose_headers=["Retry-After"]`. A cross-origin client cannot READ a
  response header unless it is exposed, so without this a 429 countdown
  silently reads undefined while the header sits on the wire.
- Local `.env` sets `http://localhost:3010`; `.env.example` ships empty.

**Test pollution, found by running the whole suite.** The first version
of `tests/test_cors.py` used `importlib.reload(app.main)` to pick up a
new setting. That replaces the module-level `app` with a NEW FastAPI
object while the rest of the suite still holds the old one -- and broke
`test_reranker_cohere::test_health_is_clean_when_nothing_is_degraded`
and `test_stream_slots::test_closing_the_body_iterator_still_releases_the_slot`,
two tests with nothing to do with CORS. Both passed in isolation. Fixed
by extracting `configure_cors(app)` so tests run the real wiring on a
throwaway app, plus one read-only test asserting `app.main` actually
calls it -- a helper can be perfect and never invoked.

#### Theme: animated light-black, animated shaded orange
- Surfaces lifted off pure black (`--bg` `#070605` -> `#0B0908`) so the
  page reads as warm charcoal with air in it, and the surface steps
  stay distinguishable on a dim panel.
- A living background: two very low-opacity warm pools on
  `body::before`, drifting on an 18s cycle. A `::before` with a
  TRANSFORM animation, not a background-position on `body` -- one
  composited layer costs nothing per frame, animating the page
  background repaints the whole document every tick.
- `.ac-surface` / `.ac-text`: a three-stop orange ramp at 220% width
  with its background-position animated, so buttons have a light source
  travelling across them instead of a flat swatch.
- Reduced motion stops the travel but KEEPS the shading and the
  background pools -- they are depth, not movement, and removing them
  would change the page's colour rather than its motion.

#### No hardcoded content
`web/lib/site.ts` holds every string the public pages render: nav,
trust points, features, the seven plain-language steps, six
architecture sections with their rejected alternatives, the stack, and
the payment roadmap. Pages are layout; content decisions sit in one
reviewable list.

#### Payments: roadmap only
Stripe and PayPal are listed with `available: false` and render as
"Not yet". No prices, because none are set. Usage is already metered
per org per day, so billing turns an existing meter into an invoice --
a checkout button that did not work would be a lie.

#### Honesty on the marketing pages
The hero demo card is labelled **"Illustration"** in the card itself.
Its timings are script values, not measurements. The data-honesty rule
governs numbers the BACKEND produced; these pages call no API, and the
one place a figure appears it is a configured constant of the system,
not a metric.

#### React 19 lint caught three real re-render bugs
`react-hooks/set-state-in-effect` flagged `AnswerDemo`, `Reveal` and
`TopBar`. Fixed properly, not suppressed:
- `lib/hooks.ts` -- `usePrefersReducedMotion` and `useScrolledPast` via
  `useSyncExternalStore`, which reads during render instead of syncing
  with an effect. The effect version re-renders twice on mount; on a
  page of twenty revealing sections that is twenty cascading passes
  before anything is visible. It also gives a defined SERVER snapshot,
  which these server-rendered components need.
- `useScrolledPast` returns a BOOLEAN, not the offset: the snapshot is
  compared by identity on every scroll event, so returning the offset
  would re-render the header once per pixel.
- The mobile menu now closes on the click that navigates rather than an
  effect watching `pathname`. The click is the cause; the pathname
  change is a consequence.

#### Still placeholders
`/chat` and the Documents/Usage screens. The app shell with the
sidebar, and the authenticated screens, are the next block (U3-U5).

### U3-U5: the signed-in product (2026-11-01)
Documents, Chat and Usage, behind an app shell. **749 backend tests
green**; frontend lint, typecheck and build clean. Verified end to end
in the browser against live Gemini + Cohere: upload -> Celery ingest ->
ready -> ask -> cited streamed answer -> metered usage.

#### The shell
`app/(app)/layout.tsx` is a route GROUP, so "(app)" never appears in a
URL. It exists to put the real auth check in ONE place: `requireUser()`
asks the backend, because a cookie's presence says nothing about
whether its token is valid, expired, or belongs to a deleted user.
`proxy.ts` also redirects, but that is UX.

- Sidebar 256px, slide-over below `lg`. Usage is admin-only and hidden
  for members -- hiding a link is a courtesy, so the page ALSO checks
  (`notFound()` for a member) and the backend returns 403. Three layers
  for one rule.
- The status chip shows the overall word only. `degraded[]` is dropped
  in `lib/server-api.ts`, server-side, so no component ever holds it
  and no later edit can render it.
- **No budget meter.** Omitted at C0; there is still no endpoint.

#### U3 Documents
The dropzone is both a drop target AND a real `<input type="file">`,
because drag-and-drop is unavailable to a keyboard or a touch screen.
Rejected files are listed with WHY (type, size, empty) above the zone
rather than vanishing.

- **Polling is conditional**: `refetchInterval` returns 3000 only while
  a row is pending or processing, and `false` otherwise. A fixed
  interval keeps a tab talking to the API forever on a page where
  nothing can change.
- Progress bars are INDETERMINATE. `DocumentStatus` is a four-value
  enum with no percentage, so a number would be invented.
- `CHIP_STATUS` maps backend `pending` to chip `queued` as an explicit
  `Record`, so adding a backend status is a type error here rather
  than a chip rendering nothing.
- A failed row fetches its own `error_message` from the detail
  endpoint -- the list response does not carry one, and "failed" with
  no reason is not actionable.

#### U4 Chat
`lib/stream.ts` reads the SSE body with `fetch` + a ReadableStream.
Frames are buffered across chunk boundaries (a network chunk can end
mid-frame) and the decoder runs with `stream: true` (it can also end
mid-UTF-8-character). Exactly one leading space is stripped after
`data:`, not trimmed -- trimming would eat the spaces between words in
a streamed answer.

- **Trace chips are built only from non-null fields.** `TraceChip`
  requires a string, so there is no way to pass it "missing". A cache
  hit shows "from cache" and no timings. `rerank_ms` is additionally
  suppressed when `candidate_count` is 0: "rerank 0 ms" is true but
  reads as an impossibly fast rerank rather than as nothing to rerank.
- Confidence and the relevance bar render only when
  `scores_calibrated` is true.
- The `citations` event REPLACES the streamed text, because a marker
  the validator dropped is still on screen until it lands.
- Only markers present in the citations payload become chips; an
  unresolved one stays plain text rather than becoming a control that
  leads nowhere.
- Stop aborts the fetch; the signal reaches FastAPI through the proxy,
  which raises CancelledError and stops paying for tokens.
- **Markdown is sanitised and images are REMOVED ENTIRELY.** An
  `<img src>` is a GET to an arbitrary host the moment an answer
  renders -- a prompt injection writing an image URL exfiltrates on
  sight, with no click. Links are limited to http/https/mailto, open
  with `noopener noreferrer`, and SHOW THEIR DOMAIN.
  `dangerouslySetInnerHTML` appears nowhere in the file.
- The Sources panel highlights the matched child inside the parent
  passage by plain substring search -- no regex, because the needle is
  document text and a mis-escaped pattern is a crash on somebody's
  PDF. If the child does not appear verbatim (children are overlapping
  token windows) the passage renders unhighlighted rather than
  guessing.
- **"Open in document" is absent**: it needs the signed-URL endpoint
  in the backlog, and a link that 404s is worse than no link.

#### U5 Usage
Four metric cards, a stacked bar chart, and an **Average latency** card
replacing the design's "Where the time goes" -- that needed per-stage
timings aggregated over a window, and the backend stores per-request
timings on the answer event plus an end-to-end daily mean. One real
number beats three invented ones.

- Deltas request DOUBLE the window in one call and compare halves.
  Null when there is no full prior period or the base is zero: a "+0%"
  would claim a comparison nobody made.
- Rates return null when the denominator is zero and render as an
  em dash.
- Below two days a sparkline is replaced by "Not enough days to show a
  trend" -- a one-point trend is not a trend.

#### Four bugs found by driving the real UI
- **The answer rendered TWICE.** After a stream ends the history query
  refetches and contains both new rows, so the live turn and the
  persisted turn were both on screen -- with the stored copy sorting
  ABOVE the question that produced it. Fixed with `Live.baseline`, a
  snapshot of the message ids that existed before the turn.
- **That snapshot could be empty.** Asking within a second of a page
  load means `history` is still undefined, and snapshotting it hid
  every earlier turn. `ask()` now resolves the history query first via
  `fetchQuery`, which returns the cache when it is already warm.
- **The bar chart rendered blank.** Bars size themselves with a
  percentage height, and a percentage against an auto-height parent
  resolves to zero. The column needed `h-full`.
- **One day of data filled the chart with solid orange**, because
  `flex-1` on a single bar takes the whole row. Capped with `max-w`.

Plus two React 19 lint errors fixed rather than suppressed: the
conversation id is now DERIVED (`chosenId ?? conversations?.[0]?.id`)
instead of copied into state by an effect, which also stopped it
fighting the user's own choice on every refetch.

#### Measured on the live run
An 823-byte Markdown policy went pending to ready inside the 3s poll.
Two questions against it: `retrieve 124 ms / rerank 1777 ms / model
first token 967 ms / candidates 5 / confidence 0.94`, then `retrieve
66 ms / rerank 618 ms / model first token 867 ms / candidates 5 /
confidence 0.81`. Usage then showed 4 questions, 1.1k tokens, 0% cache
hits, 50% refusals (the two earlier empty-corpus questions, correctly
counted) and 589 ms average latency.

**Rerank was 1777 ms and 618 ms here against the 3000 ms budget**, so
the 3001 ms measured in the B1 smoke was a cold-connection effect
rather than the steady state. Still worth watching; still not changed.

#### Known gaps
- A persisted answer renders its `[1]` markers as plain text, because
  the marker-to-source mapping is not stored (only `cited_chunk_ids`).
  Honest, but inconsistent with how a live turn looks.
- No conversation list UI: the most recent conversation is adopted and
  "New conversation" starts another. Switching between them has no
  control yet.
- Still no frontend tests. Playwright, axe and the mutation table are
  U6.

### Conversational turns, and warmer refusals (2026-11-01)
From a real transcript: "hey how are you", "how can u hlp me" and
"what is this document about" each answered "I could not find anything
in your organization's documents that answers this question." The
grounding was correct; the product read as broken. **809 backend tests
green** (+60).

**The strictness is unchanged.** Nothing was loosened in the system
prompt and no path now answers from training data. The fix sits in
FRONT of retrieval, not inside it.

#### `app/services/assistance.py` -- intercept before retrieval
A greeting is not a question about a corpus. `classify()` recognises
GREETING / CAPABILITY / THANKS and `prepare_query` short-circuits
before the rewriter, the cache, the embedder, the reranker and the
model. Measured in a test: zero embedder calls, zero rerank calls,
zero LLM calls.

- **Deterministic, not a model call.** Replies are templates filled
  from the org's own document list, so there is nothing to
  hallucinate. The alternative -- letting the system prompt chat when
  retrieval comes back empty -- would hand the model licence to answer
  from training data, which is the exact failure the pipeline exists
  to prevent.
- **Anchored matching, never substring.** A false positive is far
  worse than a false negative: missing a greeting costs a cold reply,
  but intercepting "what is the refund policy" replaces a correct
  grounded answer with small talk. Every pattern matches the WHOLE
  normalised message, backed by an 8-word cap checked first.
  `test_real_questions_are_never_intercepted` pins thirteen phrases
  that look conversational and must still reach retrieval -- "hello
  world program in the onboarding guide", "what documents do you have
  about parental leave", "who are the approvers for expenses over 500".
- **Classified on the RAW question**, before the rewriter. Once there
  is history to rewrite against, "hi" becomes something else entirely
  and the intent is no longer visible.
- The capability reply NAMES the org's ready documents, which turns
  "what can you do" from a dead end into a starting point. READY only:
  naming a document still being processed invites a question
  guaranteed to come back empty. Capped at ten with the remainder
  counted, never silently dropped.
- `list_ready_document_names` selects the filename COLUMN, not whole
  Document rows -- this runs on the answer path.

#### `MessageKind.ASSIST` (migration 0009)
Its own enum member, because both alternatives corrupt a number
someone reads. As ANSWER it would be an uncited "answer" the UI
presents as grounded, and it would enter the rewrite window as
context. As REFUSAL it would inflate the refusal rate on the usage
dashboard -- turning "people say hello more" into "quality is
dropping".

Metered as `UsageStatus.OK` with NULL tokens: it succeeded and it cost
nothing. `downgrade()` raises rather than pretending -- Postgres
cannot drop an enum value, and a downgrade that silently did nothing
would leave the schema and the migration history disagreeing.

#### Both refusal sentences reworded
A refusal is INFORMATION, not an error, and the blunt originals read
as a malfunction. Both remain single constants, because
`eval/judge.py` short-circuits on them by identity and `eval/runs.py`
records them in the run snapshot -- so a refusal stays recognisable
without string-matching prose.

**`INSUFFICIENT_CONTEXT_ANSWER` is now interpolated INTO
`SYSTEM_PROMPT`.** They were two separate literals that happened to
agree; the day someone softened one, the model would have emitted a
sentence the rest of the system no longer recognised as a refusal.

#### UI
- `kind` is now on the `citations` SSE event. `gated` alone cannot
  distinguish an assist turn -- it is neither gated nor an answer.
- An assist turn renders as plain prose with NO trace chips: nothing
  ran, so there is nothing to report. Wrapping a greeting in a banner
  would make saying hello look like a warning.
- `RefusalCard` replaces the generic info banner: amber (not red --
  styling careful behaviour as an error teaches people it is a
  malfunction), the backend's sentence, then the org's actual document
  names as chips. When nothing is READY it says so instead, because
  "try rephrasing" when there is nothing to search is a loop with no
  exit.
- `refused` now reads `kind === "refusal" || gated`, so a model-side
  refusal (safety block, zero valid citations) renders as a refusal
  too, not as an empty answer.

#### Verified live
"hey how are you" -> a greeting reply naming both uploaded documents,
no trace chips, no retrieval. "what is the capital of France" -> the
new refusal at confidence 0.02 with the document chips. "what can the
owner do on the dashboard" -> unchanged: rewritten, retrieve 62 ms,
rerank 348 ms, confidence 0.76, five cited bullets and a Sources panel
at relevance 0.76.

#### Operational note
Restarting uvicorn on Windows left an ORPHANED CHILD holding the
listen socket: the parent PID was gone, `netstat` still showed 8010
LISTENING against it, and the replacement silently failed to bind and
exited -- so the browser kept talking to the OLD code and the fix
looked broken. Found by checking the live `/openapi.json` for the new
enum value rather than trusting that a restart restarted anything.
Kill the child (`Get-CimInstance Win32_Process` by ParentProcessId),
not just the parent.

### U6: the verification checkpoint (2026-11-01)
Playwright + axe end to end, a frontend mutation table, an npm audit, a
bundle secret scan, and the two remaining ADRs. **810 backend tests and
26 end-to-end tests**; frontend lint, typecheck and build clean.

#### The harness
A SEPARATE stack, because the dev one cannot be used for this. The e2e
API runs on 8020 against its own `documind_e2e` database with fake
providers; the dev database holds Gemini vectors, and the startup guard
correctly refuses to serve queries when stored vectors came from a
different embedding model than the configured one. Tests here also
delete documents.

- `scripts/e2e_env.sh` + `scripts/e2e_api.sh`. Playwright owns the web
  server; the API is started by hand because it needs migrations first,
  and a failed run should not leave a half-migrated database.
- `CELERY_TASK_ALWAYS_EAGER` added as a SETTING (default False, e2e
  only). Ingestion runs inline, so an upload is readable the moment the
  request returns rather than the test racing a worker process. Never
  set in production -- it would make an upload block for the whole
  ingestion, which is what the async pipeline exists to avoid.
- `EMBEDDING_MODEL=fake-embedding` is as load-bearing as
  `EMBEDDING_PROVIDER=fake`. Setting only the provider left the guard
  comparing stored `fake-embedding` against configured
  `gemini-embedding-001`, so `/health` reported
  `embedding_model_mismatch` and every query returned 503 -- which
  surfaced as chat tests timing out, not as a config error.
- Burst limits matter as much as per-minute rates: the defaults are 3
  for register and 5 for login, so a suite registering six orgs tripped
  them on the fourth and the failures looked like broken auth.
- `CONFIDENCE_THRESHOLD=0.4` for e2e. FakeReranker scores by word
  overlap, so the threshold has to sit between an off-topic question
  (~0.2, shared stopwords) and an on-topic one (~0.6+). At 0.05 nothing
  was ever refused and the refusal test could not pass.

#### Four real bugs the tests found
- **A 401 from the LOGIN endpoint said "Your session has ended. Sign in
  again."** `apiFetch` treated every 401 as an expired session and
  discarded the server's message. On a login form that is both wrong
  and baffling -- the user has no session to have ended. Now auth
  attempts keep the server's message ("Those details did not match an
  account"). Caught by test 1b.
- **Pressing Stop showed a raw `DOMException`.** The abort fires during
  `fetch`, BEFORE the first byte, which is most of the wait -- and that
  rejection happened outside the try/catch that maps aborts to
  `StreamAborted`. Caught by test 7d, which itself only worked after
  the mock stopped COMPLETING the response: `route.fulfill` with a body
  ends the stream, so the first version of that test was asserting
  against an already-finished turn.
- **Every answer-cache write had been failing silently since B1.**
  `Object of type UUID is not JSON serializable` -- B1 added
  `matched_chunk_id` and the hand-written serialiser stringified only
  `document_id` and `chunk_id`. The write is deliberately non-fatal, so
  the only symptom was a cache that never stored anything and answers
  that were always recomputed. Found in an e2e API log, not by a user.
  `_citation_to_json` now iterates the dataclass instead of naming
  fields, and `test_an_answer_with_a_passage_can_actually_be_cached`
  pins it.
- **`EmptyState` rendered its title as a styled `<p>`.** An empty state
  is the only thing on screen when it shows, so it is that region's
  title, and a screen-reader user navigating by headings skipped
  straight past the one piece of text explaining why the page was
  blank. Now an `<h2>`.

#### axe: one real finding, one instructive false positive
- **Real**: inline prose links were distinguished by COLOUR ALONE
  (orange, underline on hover only). `serious` under WCAG 1.4.1, and
  invisible as links to anyone with a colour-vision deficiency --
  permanently so on a touch screen, which never hovers. Now always
  underlined.
- **False positive**: `how-its-built` reported a contrast failure on a
  near-white heading. `Reveal` holds content at `opacity-0` until it
  scrolls into view, and axe computed the contrast of a transparent
  element. Scans now run with `prefers-reduced-motion` emulated, which
  renders the resting state -- the colours a reader actually sees --
  and checks the reduced-motion path at the same time.

Zero `serious` or `critical` across landing, how-it-works,
how-its-built, login, register, `/design`, documents, chat (empty),
chat (answered, sources panel open) and usage. `minor`/`moderate` are
reported but not gated: axe counts decorative contrast among them, and
a gate that fires on those gets switched off within a week.

#### Frontend mutation table: 9 of 9 caught
`web/e2e/mutate.mjs`, same discipline as `scripts/mutate.py`.

| mutation | caught by |
|---|---|
| u1 cookie not httpOnly | auth 2 |
| u2 no sanitisation | app 8 |
| u3 allow markdown images | app 8 |
| u4 forward the browser cookie upstream | bff U4 |
| u5 remove the proxy allowlist | bff U5 |
| u6 CSRF check always passes | auth 10a/10b |
| u7 logout stops clearing the query cache | **auth 11b** |
| u8 render Usage for members | roles 4 |
| u9 draw the score bar when uncalibrated | app 12 |

**u7 SURVIVED the first time**, and the reason is the interesting part.
Test 11 checked the cookie and the redirect; removing
`queryClient.clear()` left it green because query keys are namespaced
by org and user, so a different user never reads the previous one's
entries anyway. The namespacing HID the bug. Test 11b observes the
clear directly: the same user signs back in while the documents request
is delayed five seconds, so a surviving cache would put the previous
session's document on screen immediately. With the clear, nothing
appears until the real response lands.

**u4 and u5 had no test at all** when the table was first written --
neither has a visible surface, so neither is reachable from a browser.
Rather than skip them, the pure proxy rules moved out of the
`server-only` module into `lib/proxy-rules.ts`: they hold no URL, no
secret and read no environment, so they are importable and directly
testable. `lib/backend.ts` re-exports them, so no call site changed.

#### Supply chain and secrets
`npm audit`: **0 critical, 0 high**, 16 moderate -- all in the eslint
toolchain, dev-only, rooted at one `minimatch` advisory. Nothing
moderate reaches the browser.

The client bundle (`.next/static` only -- server chunks legitimately
hold `BACKEND_URL` and are never downloaded) was grepped for `AIza`,
`JWT_SECRET`, `postgresql://`, `documind:documind`, `BACKEND_URL`,
`dm_session=`, `COHERE` and `sk-`. All clean. `sk-` matched
`contains-task-list` and `mask-type` from the sanitiser schema; a
key-shaped `sk-[A-Za-z0-9_-]{20,}` matches nothing. The scan was
checked against a control string that IS present, so a silently broken
grep would have been visible.

#### ADRs
`0004-markdown-sanitization.md` and `0005-honest-data-rules.md` join
the three from U1. All five now cite the mutation that proves them.

#### NOT verified
- **Nothing has been tested on Vercel.** No preview deployment exists,
  so every platform claim in ADR 0003 remains documentation rather than
  observation.
- **Lighthouse was not run.** axe covers the automated WCAG rules, but
  the "accessibility >= 95" line in the definition of done is unmet
  because the score was never produced.
- **Responsive widths (1440/1024/390) are still unchecked.** No
  horizontal-scroll assertion exists at any width.
- Tests run against **fake providers**, so a provider-shape change
  would not be caught here -- only by the live smoke.
- The e2e suite is **serial and slow** (~8 minutes) and runs against
  `next dev`, where Turbopack compiles routes on first visit. Two
  upload waits were raised for that reason, and one test needed its own
  240s budget. Against `next start` it would be far faster and less
  timing-sensitive.

### User-facing failure messages (2026-11-02)
A failed upload was showing the uploader the raw provider exception.
The real one from this database:

    transient provider error 429: Client error '429 Too Many Requests'
    for url 'https://generativelanguage.googleapis.com/v1beta/models/
    gemini-embedding-001:batchEmbedContents' For more information
    check: https://developer.mozilla.org/...

Wrong twice over: it tells someone who uploaded a PDF nothing they can
act on, and it discloses the embedding provider, the model name and the
upstream URL -- which the phase skill's own rule already forbade
("user-facing errors never include stack traces, backend URLs, or
internal ids"). **833 backend tests green.**

- `app/services/document_failures.py` -- `FailureCode` plus
  `classify_failure()`. Seven coarse codes, and coarse ON PURPOSE: each
  one has to map to a distinct ACTION (wait and retry, convert the
  file, pick another file, ask an admin). Codes implying the same
  action would be detail pretending to be useful.
- **Unrecognised failures return UNKNOWN, never a plausible guess.**
  Telling someone "the provider was busy, try again" when the real
  cause was a corrupt file sends them round a loop they cannot exit.
- **Classified at READ time, not stored.** A new column would have left
  every failure already in the database blank -- including the
  operator's own. Pattern matching is the price of covering history.
- `DocumentDetailResponse` **no longer has `error_message` at all.**
  The raw text stays in the row and the logs for operators. A fixed
  enum cannot carry a URL into a response by accident.
- Copy lives in `web/lib/site.ts` as `failureCopy`, with a
  `title`/`body`/`action` per code -- product copy, in the one place
  product copy lives.
- An unrecognised code still renders copy. Rendering nothing would
  leave a red "Failed" chip with no explanation, which is the state
  this change exists to remove.

**The same class of leak, one layer up.** `apiFetch` forwarded
FastAPI's `detail` straight into the UI. For a 422 that is an ARRAY of
Pydantic error objects, which rendered as "[object Object]". Now only a
plain non-empty STRING is accepted; anything else falls back to
`genericMessageFor(status)`, which says something useful per status --
a 413 resolves by choosing a smaller file, a 429 by waiting, a 403 by
asking an administrator. "Something went wrong" is true of all of them
and useful for none.

**A test that asserted the bug.**
`test_failed_document_exposes_its_error_message` asserted the raw
message WAS returned, on the stated reasoning that it was "the only
feedback the uploader gets". That reasoning was the defect. Rewritten
as `test_failed_document_reports_a_code_not_the_raw_error`, which
asserts the code is published and that no fragment of the raw text
appears under any key.

Verified against the operator's own failed row: a 1.3 MB PDF that hit
Gemini's free-tier limit mid-ingestion now classifies as
`rate_limited`, and the raw 429 text stays in the database.

#### Still open
- **Ingestion gives up on a 429 instead of backing off.** The document
  above is permanently failed and must be deleted and re-uploaded. The
  friendly message now says so, but the underlying retry is the real
  fix and is not done.
- **A failed or empty document is still invisible in CHAT.** Asking
  about it returns an ordinary refusal with no hint that the document
  carries no indexed text. That was the original confusion in the
  transcript and it is not yet addressed.
- `test_rate_limit.py::test_the_bucket_is_atomic_under_concurrency`
  fails intermittently in a full run and passes alone -- a pre-existing
  Redis-timing flake, not related to this change.

### UI backlog (backend work deferred, with its proposed design)
Each of these is a UI element omitted at C0 because no endpoint backs it.

- **Document file access -- needed for 'Open in document'.** Proposal: a
  short-lived signed URL, org-scoped, issued by an authenticated
  endpoint that 404s (never 403) for another org's document id, matching
  `GET /documents/{id}`'s existing reasoning. The URL must never carry
  the `storage_key`: that is the object's address in the bucket and is
  absent from `DocumentDetailResponse` by construction.
- **Budget endpoint -- needed for the sidebar and Usage budget meters.**
  Proposal: return `used`, `limit` and `enabled`. The counters already
  exist in Redis (`budget_key_global` / `budget_key_org`), but
  `check_budget` only ever raises `BudgetExceeded` or returns None, so
  nothing can read the remaining amount. Note
  `BUDGET_DAILY_GLOBAL_UNITS` and `BUDGET_DAILY_ORG_UNITS` both default
  to 0 = disabled, so the endpoint must report `enabled: false` rather
  than a misleading 0%, and the global key is one key for the whole
  deployment by design.
- **`chunk_count` in the document LIST response.** It is already on
  `DocumentDetailResponse` but not on `DocumentListItem`, so a table
  cannot show it without one detail request per row. This is also what
  the design skill's 'Pages' column would become: `Document` has no
  `page_count` column at all, and adding one means deciding what a page
  means for `.txt`/`.md`, which have no page concept -- ingestion
  deliberately does not fake pages for them.
- **Per-stage processing progress.** `DocumentStatus` is a 4-value enum
  with no stage and no percent, so the pipeline hint (extract, chunk,
  embed, ready) is decoration and the bar stays indeterminate.
- **`eval/calibrate.py` has no unit tests.** Carried forward from Phase
  4 and still true: it is driven only by live runs and its output is
  reviewed by hand against the run file, so a mutation in it would
  survive. Not in B1/B1b's path -- it does not read the new payload
  fields and carries its own `candidate_count` on its own dataclass --
  but it is the largest untested module in the tree.
- **`RERANKER_TIMEOUT_SECONDS = 3.0` may be too tight.** The B1 live
  smoke measured `rerank_ms = 3001` on a successful call (see above).
  Raising it trades first-token latency against the risk of falling back
  to RRF order, which also opens the confidence gate. Needs more than
  one measurement before changing.

### Phase 4 completion (2026-10-31)

#### Applied thresholds, from the n=38 live run
`CONFIDENCE_THRESHOLD = 0.3230`, `VECTOR_SIMILARITY_FLOOR = 0.5594`.
Full data in `docs/eval-baseline.md`; raw run in
`eval/runs/20261031T053310Z-full.json`; the n=24 baseline archived at
`docs/eval-baseline-n24.md`. The run was clean -- 0 reranker fallbacks,
0 rate limits, 0 errors -- so every score is a real cross-encoder score.

- **The selection rule disagreed with itself.** At n=24 one gap
  qualified, so "widest gap below the lowest answerable score" did no
  work. At n=38 THREE qualified and all three hold recall at 1.000:
  0.1324 (FP=11), 0.2653 (FP=6), 0.3230 (FP=4). The rule returns the
  widest, which spends precision on margin without being asked.
  Operator chose the highest: 7 fewer false answers, margin 0.0165
  instead of 0.207. `select_threshold` now reports every qualifying gap
  with its confusion matrix. The old 0.4162 is strictly WORSE than
  0.3230 here (same 4 FPs, one fewer TP).
- **The overlap is unchanged and irreducible.** Worst answerable 0.3395
  < best refusable 0.8730. Four should-refuse cases sit above the lowest
  answerable score at any threshold. A cross-encoder scores relevance,
  not sufficiency.
- **The prompt refuses, not the gate.** End-to-end 15/17; with the gate
  forced open the model declined the same 15. The two failures are the
  two long-standing disputed cases. No sufficiency check built -- the
  measured gain is at most 2 of 17, both arguable.
- **Retrieval is not the bottleneck, and the number is weak anyway.**
  recall@8 = 1.000, MRR 0.985 over 33 scored cases. Stated with its
  limit: 40 children per tenant means the top 8 is a fifth of the
  corpus. This proves wiring, not scale.
- **The strongest new finding: 2 of 3 `spans_two_chunks` cases answered
  HALF the span.** The parent sent to the model contained both halves
  (the chunker test guarantees it); the answer used the part nearest the
  retrieved child, and both read as good answers. Parent-child chunking
  is this architecture's central retrieval decision and this is the
  first evidence the model does not always use what it is given.
- `acme-rollback-cluster-flag` (0.3395) was a FALSE REFUSAL at 0.4162.
  It now passes the gate and nothing has measured what the model does
  with it. Every generated answer in the baseline was produced at
  0.4162, which is why the run file records the thresholds in force.
- **Hand grading found a real judge defect, and the judge is still
  UNKNOWN.** On `acme-deploy-friday` it called "No, production deploys
  run Tuesday through Thursday" SUPPORTED, after being told in as many
  words that *"Deploys run Tuesday to Thursday" does not state what
  happens on Friday*. Diagnosis was PLACEMENT, not wording: the rule sat
  in a trailing `Rules:` list while the definition of `supported` said
  "is every claim STATED in the CONTEXT", and a leading "No" does not
  read as a claim. The rule now lives inside that definition and the
  example is worked through to its verdict. Verified with 3 targeted
  re-judges: 3/3 against hand grades, and the fixed judge's own reason
  is "the context states when deploys run but does not explicitly forbid
  Friday afternoon deployments".
- **Agreement is 3/3 and reported as UNKNOWN**, because
  `Agreement.meets_bar` returns None below 10 grades. Three perfect
  grades is not evidence. **And more runs will not help**: the
  canned-refusal shortcut handled 16 of 19 verdicts, so only 3 were
  model calls. Reaching n=10 needs judging the forced-open answers for
  ANSWERABLE cases too -- ~21 more model calls per run, which is a
  decision, not a detail.

#### Harness (Part 2)
- `eval/metrics.py` -- recall@k, MRR, citation-page correctness as pure
  functions over a ranked list. The old `retrieval_hit` bool could not
  tell rank 1 from rank 47. TWO ranked lists per case, because one
  cannot diagnose: candidate recall 1.0 + reranked 0.0 means the
  cross-encoder demoted it; both 0.0 means neither leg found it.
  Relevance is labelled `(document, page)`, never chunk id -- ids are
  regenerated on every ingest and by any `CHILD_TOKENS` change, so an
  id-pinned set would go stale SILENTLY and read as a retrieval
  collapse. "No support labelled" is None, never 0.0.
- `eval/runs.py` -- `eval/runs/<timestamp>-<label>.json` with a config
  snapshot. The old code dumped a bare list to one GITIGNORED
  `eval/results.json`, so each run erased the last and the evidence for
  a live threshold existed nowhere. Secrets excluded by an ALLOW-LIST.
  The first redaction rule matched "token" as a substring and silently
  dropped CHILD_TOKENS/PARENT_TOKENS/LLM_MAX_TOKENS/CONTEXT_TOKEN_BUDGET
  -- the four settings that decide what a recall figure means. Caught by
  its own test.
- Golden set has a `version` (now "2"); absent or blank is a load error.
- Judge: own `JUDGE_MODEL`/`JUDGE_TEMPERATURE`(0.0)/`JUDGE_MAX_RETRIES`
  and `get_judge_llm()`. One retry on invalid JSON WITH THE FAILURE FED
  BACK -- a verbatim resend at temperature 0 reproduces the same
  unparseable reply. Cached on sha256(judge model, grading prompt,
  question, context, answer), NOT on case id: the id is stable across
  runs while the answer is not. UNPARSEABLE is never cached.
- `--smoke`: nine cases, one per retrieval shape and gate regime,
  including the isolation twin and the only path through `check_rubric`.
  Records `sufficient_for_calibration: false`.

#### Protection (Parts 4-5)
- `app/services/rate_limit.py` -- Redis token bucket in Lua, per org.
  Token bucket over fixed window (300 at 11:59:59 + 300 at 12:00:00
  never exceeds a fixed window) and over sliding-window-log (one member
  per request to answer what one float answers). ONE script per
  decision: the read-modify-write version allows more than the capacity
  whenever two requests interleave, so it fails open under load, which
  is when it is needed. Time from Redis `TIME` inside the script --
  several API processes mean several clocks.
- Stream slots are a TIMESTAMPED SORTED SET, not INCR/DECR. A worker
  killed mid-stream never runs its release; with a counter that slot is
  gone until someone deletes a key by hand. Expiring members self-heal.
- 30/min answers, 300/min other, 3 concurrent streams; per-IP on
  login/register (a per-ACCOUNT limit cannot see credential stuffing).
  429 carries `Retry-After` rounded UP; the body names no limit, key or
  org. X-Forwarded-For ignored unless `TRUST_FORWARDED_FOR` -- a
  spoofable client IP is worse than none.
- Redis down is ASYMMETRIC: auth and reads fail OPEN, the answer
  endpoint fails CLOSED onto a per-process bucket. `/health` reports
  `rate_limiter_degraded`.
- The SSE slot is released in a `finally` around the generator, covering
  completion, exception, and `GeneratorExit`/cancellation.
- `usage_events` (migration 0008), composite index (org_id, created_at)
  with org_id LEADING. Token columns NULLABLE because unknown and zero
  are different facts. Counts come from Gemini's own `usageMetadata` via
  a CALLER-SUPPLIED `usage_sink` dict -- not tiktoken (not Gemini's
  tokenizer) and not an attribute on the provider (a singleton would mix
  two tenants' tokens). The sink is cleared per retry attempt.
- `usage_service.record()` NEVER raises: a usage row is a byproduct of
  work the user already received. 429s are deliberately NOT metered --
  an attacker's rejected requests would each buy a DB write, making the
  limiter a DoS amplifier. Budget rejections ARE. Cache hits do not
  count against the budget: counting them would make the thing that
  saves quota consume it.
- `GET /api/v1/usage` -- admin only, org-scoped by the TOKEN. There is
  no `org_id` parameter at all, so "usage for another org" is not a
  request this API can express.

#### Mutations (Part 7)
12 applied one at a time on a throwaway branch, 12 caught, 0 survivors
-- after ONE REAL GAP was found and closed. See
`docs/mutation-testing.md` and `scripts/mutate.py`.

The gap: `leak-slot-on-disconnect` was reported CAUGHT by the first
sweep, but the catch was a false comfort -- the failing test was the
persistence one, while `test_..._when_the_client_disconnects`, written
for exactly this, PASSED with the mutation in place. `TestClient`
buffers the whole response, so abandoning its iterator never cancels
anything server-side. A real server calls `aclose()` on the body
iterator, raising `GeneratorExit` at the suspended yield; a `finally`
runs, a trailing statement does not.
`test_closing_the_body_iterator_still_releases_the_slot` now drives the
route's own `body_iterator` and was confirmed failing against the
mutation before being committed. **A test named after a failure mode is
not evidence that it exercises that failure mode.**

#### Two test-isolation bugs found by this work
- **The suite's app lifespan read the DEV database.** `app/main.py`
  opens its own `SessionLocal()`, which the `get_db` override does not
  cover. Running the harness offline (`EMBEDDING_PROVIDER=fake` writes
  chunks tagged `fake-embedding`) tripped the embedding-model guard and
  turned four SSE tests and `/health` red with no change to the code
  they exercise -- the suite was green or red according to a database it
  does not own. Fixed in `conftest.py`;
  `test_the_startup_guard_reads_the_TEST_database` pins it.
- **The limiter was shared across tests and needed a live Redis.**
  `get_limiter()` builds a `RedisLimiter`, so 34 route tests silently
  required Redis; and one shared register bucket (3/min per IP) meant
  the fourth test that registered a user got a 429 -- which test that
  was depended on execution order. An autouse fixture installs a fresh
  `InMemoryLimiter` per test, with limiting still ENABLED. The budget
  tests fake the UTC day, because the global budget key is one key for
  the whole deployment by design.

#### Carried-forward gaps closed
- `llm.py` concatenated every response part with no `thought` filter in
  two places. Latent (Gemini sends thought parts only under
  `includeThoughts`, and `thinkingBudget: 0` is a 400 on this model) but
  it would splice private reasoning into a cited answer, the stored
  `Message` row and the cache the day anyone enables it.
- `MessageResponse` now exposes `kind`, so a client can tell an answer
  from a refusal from an outage instead of pattern-matching prose. NULL
  stays NULL for user turns and pre-0006 rows.

### Known gaps carried into the UI phase
- **`eval/calibrate.py` has no unit tests.** It is driven by live runs
  and a mutation in it would survive; its output is reviewed by hand
  against the run file.
- **Judge agreement is n=3.** 3/3 after the prompt fix, which is not
  enough to conclude; `meets_bar` is None below 10. The shortcut that
  makes the judge cheap is what makes it hard to measure.
  `JUDGE_MODEL` still equals `LLM_MODEL`, so the judge grades its own
  output -- a config change away from not doing so.
- **Nothing has been measured at the newly applied 0.3230.** Every
  generated answer in the baseline ran at 0.4162.
- **Answer completeness over a parent is unproven** (the
  `spans_two_chunks` finding above).
- **Retrieval at scale is unmeasured**: recall@8 = 1.000 over 40
  children per tenant.
- The reranker breaker clears only on process restart, which is correct
  for a config fix but means a provider-side 401 later resolved still
  needs a restart.
- No live test covers a refusal path or cache-hit behaviour end to end.
- The in-memory limiter fallback is per PROCESS: with N API processes an
  org gets N buckets. It bounds the worst case to N x limit instead of
  infinity, and is not a correct distributed limiter.

### Phase 4 audit + golden-set expansion (2026-10-31)
Step 0 audit of the Phase 4 playbook, then Part 1. **564 tests green**
against real Postgres + pgvector.

- **Audited, not assumed.** Missing outright: retrieval metrics
  (recall@k, MRR exist nowhere -- the only "recall" in the tree is gate
  classification recall), rate limiter, budget guard, `usage_events`,
  mutation testing. **Broken**: `eval/calibrate.py:896` dumps a bare
  LIST to one fixed `eval/results.json` -- no timestamp, no models, no
  thresholds, no commit, and each run overwrites the last, so two runs
  cannot be compared. Partial: judge (no retry on invalid JSON, no
  content-hash cache, no separate judge-model Setting, no recorded
  agreement rate), harness (no `--smoke`, no per-org table).
- **Golden set 26 -> 40 questions**, gate-scored 24 -> 38 (21 answer /
  17 refuse, ratio 0.55). Near-miss : off-topic is now 10:2.
- **Four categories ADDED alongside the original eight**, not renamed:
  `exact_term`, `table`, `code`, `spans_two_chunks`. The content for
  table and code already existed but was filed as `answerable_direct`,
  so no per-type row could ever show "every table case fails while the
  prose cases pass". Renaming to the playbook's literal taxonomy would
  have churned `loader.py`, `thresholds.py`, `calibrate.py`, 33 tests
  and every case id quoted in `docs/eval-baseline.md` for no
  measurement gain.
- **A third acme document** (`vendor-security-review.pdf`, 4 pages,
  generated by `eval/build_corpus.py`) and a second globex document.
  New documents rather than new sections in the existing two:
  regenerating a PDF reflows its pages, and eight existing cases pin
  `expect_pages` against the current layout -- a correct citation would
  have started scoring as wrong.
- **`spans_two_chunks` is verified against the real chunker, not
  asserted.** Page 3 of the new PDF is ONE 192-token parent under a
  single heading, split into children of 124 and 127 tokens; steps 1-4
  live only in child 0 and steps 7-10 only in child 1. Each case needs
  one fact from each side, so the embedded child that retrieves cannot
  contain the whole answer and only the parent sent to the LLM can.
  `test_spans_two_chunks_cases_really_span_two_chunks` chunks the
  corpus with the production chunker and fails if any single child
  carries every `must_mention` term. It **caught one of my own labels**:
  `acme-vsr-tier3-extra-evidence` originally required "tier 3", which
  appears in the page-3 chunk as well, so the case spanned nothing. The
  rubric now requires "tier 1", which exists only in the page-2 table.
- **Three set-level rules moved from tests INTO the loader**: isolation
  probes need `forbid_phrases`, need a twin asking the identical
  question in another org, and that twin must ANSWER it; and near-miss
  refusals must be at least twice the off-topic ones. Asserted only in
  tests, they caught rot in CI but let every interactive
  `load_golden_set()` -- the eval harness included -- run on a rotted
  set.
- **The acme -> globex isolation direction was never measured.** Both
  original probes ran globex -> acme. Added `acme-asks-contractor-expiry`
  and re-filed `acme-parental-leave` (it was `unanswerable_off_topic`,
  but globex answers the identical question, which makes it an isolation
  probe) with `forbid_phrases` so a leak is detectable.
- **`docs/eval-baseline.md` is marked STALE.** The thresholds in
  Settings are unchanged and still applied; the evidence for them was
  fitted at n=24 and no longer covers the 38-case set. Re-running costs
  live Gemini + Cohere calls, so it is the operator's call.

### Phase 4 so far
- **Golden set** in `eval/` -- 26 questions, 24 gate-scored (12/12
  balanced), two generated 4-page PDFs (table, fenced code block,
  running headers/footers), `ambiguous` and `synthesis` types,
  `expect_pages` validated against real extracted text. Labels are
  hand-written, never LLM-generated: "is this answerable from the
  corpus?" is the exact judgement the gate is graded on.
- **Thresholds applied from measurement**, by a rule fixed in advance
  (`eval/thresholds.py`): `CONFIDENCE_THRESHOLD = 0.4162`,
  `VECTOR_SIMILARITY_FLOOR = 0.6175`. Full data and the rule's rationale
  in `docs/eval-baseline.md`. Both provisional at n=24, both
  env-overridable, neither transferable to another provider.
- **The gate's fallback regime now has a signal.** `used_fallback=True`
  previously opened the gate unconditionally, so a dead reranker
  answered EVERYTHING -- during exactly the incident when nobody is
  watching quality. `Candidate.vector_similarity` now carries cosine
  from the vector leg (free: the leg already computes that distance to
  order by) and the floor is applied there. A floor of 0 restores the
  old permissive behaviour, which is the default for anyone who has not
  calibrated one.
- **The central finding: the classes OVERLAP and no threshold separates
  them.** Worst answerable 0.5530 sits below best refusable 0.8730. A
  cross-encoder scores RELEVANCE, and `acme-rollback-migrations-how`
  (0.8730) and `acme-deploy-friday` (0.6722) are maximally relevant to
  questions they do not answer. Reported as *irreducible* rather than
  averaged away. The gate is a cheap filter, not a correctness
  guarantee.
- **What actually refuses the hard cases is the prompt.** With the gate
  forced open, the model declined 11 of 12 should-refuse questions
  unaided -- including the 0.8730 case, where it answered "the provided
  documents do not contain instructions on how to perform that manual
  change". The one dispute is `acme-deploy-friday` ("No, deploys run
  Tuesday through Thursday"): an unsupported inference on the stricter
  reading, SUPPORTED per the judge. Candidate for the `ambiguous`
  bucket.
- **`eval/judge.py` judges two axes, not one.** Asked for a single
  verdict it got both decisive cases wrong -- grading a correct refusal
  as SUPPORTED, which was true of every claim in it and beside the
  point. `asserts_answer` and `supported` are now separate and the label
  is derived. A judge asked for one label answers whichever question is
  easier.
- The eval throttles RERANK CALLS, not the loop around them (a Cohere
  trial key is 10/min, and step 2 made three reranks per case), and uses
  a 25s reranker budget rather than production's 3s -- a timeout in the
  eval scores the case on RRF, calibrating the fallback while claiming
  to calibrate the gate.

Next: Part 2 harness gaps (recall@k/MRR, run storage with a config
snapshot, --smoke, judge hardening), then Parts 4-7 (rate limiting,
budget guard, usage logging, mutation testing). Blocked on an
operator decision: whether to re-calibrate at n=38, which is a live
run.

### Free-provider migration (Gemini + Cohere)
Goal: move off OpenAI/Anthropic onto Gemini (embeddings + answers) and a
Cohere trial key (reranking), then prove the pipeline with ONE live
end-to-end run. **No provider code had EVER been run against a real API
before this** -- every provider test injects a fake client, the
`anthropic` SDK was never installed, and `requirements.txt` line 19 was
`pgvector==0.3.4boto3==1.35.49` (unparseable, so a fresh
`pip install -r` always failed). All three are now fixed/underway.

- **STEP 2 done - Gemini embeddings.** `GeminiEmbeddings` in
  `app/services/embedding.py`, REST over httpx (not the google-genai SDK:
  httpx is already a dependency and `httpx.HTTPStatusError` exposes
  `.response.status_code`, which is exactly what `classify_error()`
  reads). Model is `gemini-embedding-001`, NOT `gemini-embedding-2` --
  the newer model is multimodal and self-normalizing but **dropped the
  `taskType` parameter**, and asymmetric document/query embedding is
  worth more here than unused multimodality.
  - `EmbeddingTask.DOCUMENT` / `.QUERY` -> `RETRIEVAL_DOCUMENT` /
    `RETRIEVAL_QUERY`. Defaults to DOCUMENT; the single query call site
    (`query_service.prepare_query`) passes QUERY and a test pins it.
  - `outputDimensionality=1536` requested AND the returned length is
    asserted against `EMBEDDING_DIMENSIONS`, failing loudly.
  - **Vectors are L2-normalized by us.** `gemini-embedding-001` only
    normalizes its full 3072-d output; below that it Matryoshka-truncates
    and the prefix is not unit length. Cosine is magnitude-invariant so
    this is not what makes retrieval work today -- it is what stops it
    breaking under `vector_ip_ops`/`vector_l2_ops`.
  - Auth via the `x-goog-api-key` **header**, never `?key=` (a query
    param lands in every proxy and access log).
  - `RateLimiter`: flat minimum interval, not a token bucket -- a
    bucket's burst allowance is precisely what trips a per-minute cap at
    the start of an ingestion. Throttles HTTP **requests**, not texts.
  - **Bug fixed in `classify_error()`**: the transient-hint list had
    `"connection"`, but `httpx.ConnectError` lowercases to
    `"connecterror"`. Every DNS failure and refused connection was
    classified PERMANENT and the retry loop silently never ran.
  - **Startup guard** `check_embedding_model_consistency()` runs once in
    the FastAPI lifespan. On a mismatch it does NOT exit the process --
    it records the condition and only the query endpoint returns 503, so
    uploads/auth/document management stay up. Deliberately **not**
    org-scoped: it is an operator-level deployment check.
- **STEP 3 done - Gemini streaming LLM.** `GeminiLLM` in
  `app/services/llm.py`, REST over httpx.
  - Streams `:streamGenerateContent?alt=sse`. **`alt=sse` is load-
    bearing**: without it Gemini buffers one JSON array and "streaming"
    delivers nothing until the answer is finished.
  - Still **no `tools` key** -- absent, not empty, and a test strips
    comments from the source before asserting the word never appears.
    System prompt rides `systemInstruction`, not a prepended user turn,
    so grounding rules and retrieved context never share a channel.
  - Error hierarchy: `LLMRateLimitError` (429/503/529), `LLMTimeoutError`
    (408 + connect/timeout/network/protocol), `LLMSafetyBlockedError`,
    `LLMEmptyResponseError`, `LLMUpstreamError`. `safe_answer_for()` maps
    each onto its own user-facing sentence -- "something went wrong" is
    true of all of them and useful for none, since a rate limit resolves
    by waiting, a safety block never does, and an empty response usually
    resolves by rephrasing.
  - **Two distinct refusal channels**: `promptFeedback.blockReason` (the
    INPUT was rejected, no candidate ever produced) and
    `finishReason` in {SAFETY, RECITATION, BLOCKLIST, PROHIBITED_CONTENT,
    SPII, IMAGE_SAFETY} (generation started, then cut off). `MAX_TOKENS`
    is deliberately NOT a refusal -- truncated text is still readable.
  - **Empty stream raises** rather than returning "". That is the hung-
    stream failure mode: zero tokens and a clean finish leaves the user
    watching a blank bubble forever.
  - `generate_answer` **discards partial text** on any failure. Half a
    refused answer reads exactly like a whole one; the route's final
    `citations` event is authoritative and supersedes what was printed.
    No citations are attached either -- provenance for text that was
    never generated is fabricated.
  - A malformed SSE frame is logged and skipped, not fatal: a slightly
    short answer beats an exception over one bad chunk.
  - `LLM_MODEL` / `REWRITE_MODEL` both default to `gemini-3.5-flash-lite`
    (NB: Gemini 2.5 models are access-restricted for new projects; the
    current line is 3.1/3.5/3.8).
- **STEP 3 follow-ups (directed review).**
  - **Two timeouts, not one.** `LLM_TIMEOUT_SECONDS` (30s) is the
    PER-READ stall limit handed to httpx; `LLM_TOTAL_TIMEOUT_SECONDS`
    (120s) is a real deadline enforced with `asyncio.timeout` around the
    whole stream. httpx's `timeout=` alone never fires on a model that
    emits one token every 25s forever -- each read is fast, the call
    just never ends.
  - **One retry before the first token** (`LLM_MAX_RETRIES=1`,
    `LLM_RETRY_BASE_SECONDS=1.0`, exponential). Retryable only while
    `produced_text` is False: once bytes are on the client's wire a
    retry would deliver the opening of one answer followed by the whole
    of another. 400s, safety blocks and empty responses are never
    retried.
  - **Malformed SSE frames are counted, not logged per-frame**, and the
    single summary carries a per-call `request_id` (uuid4[:8]) as both
    message text and a `logging` extra.
  - On a mid-stream failure the **safe message** is what lands in the
    `Message` row (`cited_chunk_ids` NULL) and **nothing is cached** --
    `should_cache()` refuses twice, on `errored=True` and on the empty
    citation list.
- **Bug fixed: Alembic was silencing the application.** `alembic/env.py`
  called `fileConfig()` without `disable_existing_loggers=False`, whose
  default is True -- so every already-created `app.*` logger was
  DISABLED the moment migrations ran in-process. Found because three LLM
  tests passed alone and failed in the full suite.
  `tests/test_logging_survives_migrations.py` pins it.
- **STEP 3 follow-ups, round 2 (directed review).**
  - **The deadline is now ONE budget for the whole request.** Fixed as
    an absolute instant in `stream()` before the first attempt and
    passed to each attempt as `asyncio.timeout_at(deadline)`. A
    deadline that restarts per attempt is not a deadline: with one
    retry a "120s" limit permitted 240s plus backoff. Backoff time
    counts against it too -- if the sleep would consume the remaining
    budget we fail immediately instead of sleeping and then failing.
  - `LLMDeadlineExceededError` subclasses `LLMTimeoutError` (so the
    user message stays "took too long") but is **never retried**:
    there is no budget left to retry inside of.
  - **Retry-After is honoured when short.** `LLMRateLimitError` carries
    `retry_after`, parsed from the header as delay-seconds OR an
    HTTP-date (RFC 9110 allows both). Under
    `LLM_RETRY_AFTER_MAX_SECONDS` (5s) it replaces our guessed backoff
    -- the server knows better. Over it we **fail fast** with the
    rate-limit sentence: honouring 90s inside a request handler is a
    hang with extra steps. A malformed header falls back to backoff.
  - **`messages.kind`** (`answer` | `refusal` | `error`, migration
    0006, nullable). `history_for_rewrite` excludes refusals and errors;
    `list_messages` still returns them for display. NULL passes the
    filter, covering both user turns and pre-migration rows. The
    exclusion is applied BEFORE the limit so a run of refusals cannot
    empty the window. REFUSAL = declined on content (gate, safety
    block, zero valid citations); ERROR = infrastructure (rate limit,
    timeout, empty response).
  - `generate_answer` now yields a frozen `AnswerOutcome` dataclass
    instead of a tuple that had grown twice.
- **Bug fixed: a turn's answer sorted ABOVE its question.** Postgres
  `now()` is `transaction_timestamp()` -- constant for the whole
  transaction -- and `persist_turn` writes both rows in one commit, so
  they shared a `created_at`. The sort then fell through to
  `Message.role`, which is not a tie-break but an alphabetical sort,
  and `'assistant' < 'user'`. Fixed with an explicit `_TURN_ORDER`
  tie-break (works for existing rows) plus migration 0007 moving the
  column default to `clock_timestamp()` (fixes it at the source for new
  rows).
- **STEP 4 done - Cohere v2 reranker.** `HostedReranker` existed but had
  never met a real API.
  - Wire format checked against the published v2 contract rather than
    remembered: `documents` is a list of STRINGS (v1's document objects
    are a 400), per-document truncation is `max_tokens_per_doc` (v1
    called it `max_chunks_per_doc`), at most 1000 documents. The limit
    is now sent explicitly so a provider changing its own default cannot
    change what the cross-encoder actually read.
  - Typed failures replace one blanket `except Exception`. Every failure
    still ends in the same place (RRF fallback, answer anyway), so the
    type cannot change what the USER gets -- it changes what the
    OPERATOR gets. 429 -> `RerankerRateLimitError`, transient, warning.
    401/403 -> `RerankerAuthError`; 400/404/422 -> `RerankerRequestError`
    -- both permanent, and both **trip a breaker**: ERROR logged exactly
    once, and every later query skips the network instead of paying
    latency to fail identically. That was the real bug: a bad key
    degraded to RRF silently, and because `used_fallback` OPENS the
    confidence gate, every answer afterwards was built on uncalibrated
    RRF scores with one WARNING to show for it.
  - A malformed body is `RerankerProtocolError` and deliberately does
    NOT trip the breaker: a proxy error page and a wrong base URL arrive
    looking identical, and only one of them resolves on its own.
  - **Blank documents are filtered before sending** (one of them is a
    400 for the whole query) through a PARALLEL list, because a returned
    index refers to the list as submitted -- filtering without remapping
    would attach scores to the wrong chunks, in a plausible-looking
    order, with the citations following them. Duplicate and non-integer
    indices (bool is an int subclass, and `True` indexes position 1) are
    rejected too.
  - **No client-side throttle**, unlike embedding's `RateLimiter`.
    Reranking runs inside a user request with a 3s budget; a trial key's
    10 req/min means 6s spacing, which would guarantee the timeout the
    throttle was meant to avoid. Throttling is right for ingestion,
    where wall-clock is free, and wrong here.
  - `rerank-v3.5` kept as the model: 4k context is irrelevant for
    125-token children. NB the v5.0 line advertises **calibrated**
    relevance scores, which is what would make `CONFIDENCE_THRESHOLD` a
    meaningful absolute threshold rather than a guess -- worth revisiting
    when Phase 4 calibrates the gate.
- **STEP 5 done - one shared HTTP connection pool.** Every provider was
  building its own client per call, and a client owns a connection pool,
  so a client per call is a pool per call -- which is no pool at all:
  a fresh TCP connection and TLS handshake (2-3 round trips) before any
  payload moved, on every embedding batch, every rerank and every
  answer.
  - `app/core/http.py`. Two pools, because there are two execution
    contexts: `httpx.AsyncClient` for the API process (LLM, reranker),
    `httpx.Client` for the Celery worker (embeddings).
  - **The async registry is keyed by the RUNNING LOOP, not a module
    global.** An AsyncClient's pooled connections belong to the loop
    that opened them, so a bare global works perfectly in production
    (one loop, forever) and raises "Event loop is closed" under
    pytest-asyncio, which builds a loop per test -- invisible where it
    matters, baffling where you debug. `WeakKeyDictionary` rather than
    `id(loop)` because CPython reuses `id()` after collection, which
    would hand a new loop a pool belonging to a dead one.
  - **Timeouts moved onto the REQUEST, not the client.** The reranker
    gets 3s and the LLM 30s, so a client-level timeout would force one
    pool per timeout value and undo the sharing. The client keeps a
    never-None backstop (None means "wait forever" in httpx), and a test
    pins that the LLM still passes its own -- otherwise a configured
    `LLM_TIMEOUT_SECONDS=60` would be silently capped at the pool
    default with every streaming test still green.
  - Lifecycle: the FastAPI lifespan closes the loop's client on
    shutdown; `worker_process_init` **drops** (never closes) any pool
    inherited across fork, since prefork forks after importing task
    modules and two processes writing into one TLS socket interleave
    their bytes. Closing there would tear down the parent's connections.
  - 9 fake-client signatures needed `timeout=None`: they are INJECTED
    clients, and a fake that would not accept it turned every call into
    a `TypeError` the provider classified as an upstream failure -- 47
    tests failed without one of them mentioning a timeout.
- **STEP 6 done - the query embedding no longer blocks the event loop.**
  `prepare_query` threaded both of its DB calls through `run_blocking`
  and then called `embedder.embed(...)` inline. `embed()` is
  SYNCHRONOUS -- a blocking httpx POST preceded by
  `RateLimiter.acquire()`'s `time.sleep` -- so it froze the whole API
  process for its duration: not slowing the question being asked, but
  stopping every other user's stream mid-token, for up to
  `EMBED_TIMEOUT_SECONDS` in the worst case.
  - Nothing in the suite could have caught it, which is the more useful
    half: every test injects a fake embedder that returns instantly, so
    "blocking" had no observable consequence. The new tests pin the
    dispatch (a flag set only inside `run_blocking`), the worker-thread
    identity, and the consequence (a ticker that keeps counting through
    a real `time.sleep(0.3)`). All four were confirmed to FAIL against
    the inline call before being committed.
  - NOT fixed by making `embed()` async: the same method is called from
    the synchronous Celery task, which would then need an event loop per
    batch -- and `asyncio.run()` per batch would defeat STEP 5's pool
    outright, since a new loop gets a new pooled client every time.

### STEP 7 - the live run (Gemini verified, Cohere blocked)
`scripts/smoke_live.py`, run deliberately and never from pytest. Legs:
`config`, `models`, `embed`, `answer`, `rerank`. Each leg is one request
(two where the point is comparing two responses); nothing loops, because
Gemini's free tier counts requests per minute and a Cohere trial key
allows 10 rerank req/min and 1000 calls a month. It reports keys as
"present (NN chars)" and never prints one.

**Measured against the real API:**
- `models` leg: 60 models visible to this key. `gemini-embedding-001`
  and `gemini-3.5-flash-lite` are both present, so the configured names
  are right for THIS project (availability is per-project -- the 2.5
  line is restricted for new projects).
- **`supportedGenerationMethods` is INCOMPLETE and must not be trusted.**
  `gemini-3.5-flash-lite` lists only `generateContent`, `countTokens`,
  `createCachedContent`, `batchGenerateContent` -- no
  `streamGenerateContent` -- and yet `:streamGenerateContent?alt=sse`
  streams correctly. The `models` leg deliberately does not check for
  it; doing so would reject a model that works.
- `embed` leg PASSES: 1536 dims as requested via
  `outputDimensionality`, unit norm (our own L2 normalization), and
  **`taskType` is genuinely honoured** -- the same sentence embedded as
  RETRIEVAL_DOCUMENT vs RETRIEVAL_QUERY gives cosine **0.8813**, not
  1.0. That is the asymmetric-retrieval decision confirmed rather than
  assumed; had Gemini ignored the parameter the vectors would have been
  identical and the whole choice a no-op that errors nowhere.
- `answer` leg PASSES: a grounded answer streamed in 2 chunks and cited
  `[1]` correctly, through the real `build_prompt` output (1123-char
  system, 412-char user), not a hand-written prompt.
- **Thinking: measured, and NOT a problem for this workload.** The model
  metadata says `"thinking": true`, which raised three concerns --
  reasoning eating the 1024-token `LLM_MAX_TOKENS`, latency before the
  first visible token, and `thought` parts leaking into the answer.
  Measured on a grounded prompt: TTFT **2.89s**, total 2.98s,
  `thoughtsTokenCount` absent, **zero** thought parts. Gemini does not
  return thought parts unless `thinkingConfig.includeThoughts` is set,
  so the leak is latent, not live. `thinkingBudget: 0` is **rejected
  with a 400** on this model, so disabling thinking is not even an
  available lever.
  - Still latent and worth knowing: `llm.py` concatenates every part's
    `text` with no `thought` filter (two places). Harmless today
    because those parts are not sent; it would become an answer-
    corrupting bug the moment anyone enables `includeThoughts`.
- **Observed live: the first attempt intermittently fails with a
  connection-level error classified `LLMTimeoutError`, and the single
  pre-first-token retry absorbs it.** That is STEP 3's retry doing
  exactly its job. But the log said only "(LLMTimeoutError)", which
  cannot distinguish a read stalled for 30s from a connection refused in
  20ms -- the same class, nothing else in common. The retry warning now
  carries the cause message too, with a test pinning that the API key
  never appears in it (auth rides the `x-goog-api-key` header, never the
  URL, which is the only reason logging httpx messages is safe).

- `rerank` leg PASSES against live Cohere v2. Three chunks submitted
  deliberately mis-ordered, the answer-bearing one last; it came back at
  **rank 1 with score 0.7400** -- a result passthrough cannot produce by
  accident, so this exercises the real wire format AND the index remap,
  not just connectivity. The v2 payload (string documents,
  `max_tokens_per_doc`) was accepted as written.
  - **First real calibration data point for `CONFIDENCE_THRESHOLD`.** A
    clearly-on-topic match scored 0.74 against a default threshold of
    0.3. One point is not a calibration, but it says the default is at
    least not absurd, and that the gate's useful range is well below a
    strong match. Phase 4 needs the other half: what an off-topic
    question scores.
- **The key arrived as `COHERE_API_KEY`, which the app did not read.**
  Settings wanted `RERANKER_API_KEY`, so the key resolved to None, the
  hosted reranker was never constructed, and reranking was simply OFF --
  nothing raised, nothing logged. The same silent-degradation class
  STEP 4 was built to eliminate, one layer up in configuration. Fixed
  twice over: `RERANKER_API_KEY` now also reads `COHERE_API_KEY` (the
  canonical name stays provider-neutral because `RERANKER_BASE_URL` is
  configurable, but COHERE_API_KEY is what a person writes down), and
  `check_reranker_config()` runs in the lifespan to warn when a key is
  set while the provider is not `hosted`. `get_reranker()` already
  failed loudly the obvious way round; the inverse was the silent one.
- **The suite is now offline BY CONSTRUCTION.** Settings read `.env`,
  and `.env` is where real keys live -- so flipping
  `EMBEDDING_PROVIDER=gemini` for this run would have made the next
  plain `pytest` start billing for network calls through the provider
  factories. A session fixture in `conftest.py` forces all three
  providers to `fake` and clears the `lru_cache`s that would otherwise
  hold a provider built before it ran. Confirmed by runtime: 487 tests,
  same 150s as before the flip.

### The full end-to-end run (done, live)
Register -> login -> upload -> Celery ingest -> ask -> cited SSE answer,
driven over the real HTTP API against live Gemini + live Cohere:
- Ingestion: a 644-byte markdown policy became **5 parents / 5 children**
  in **1.05s**, embedded in a **single** `batchEmbedContents` request --
  so batching holds, and the STEP 5 pool served it.
- The answer was grounded and cited twice: "Expense reports must be
  submitted within thirty days of the end of travel [1]. If submitted
  after this window, you require written approval from a department
  director [1]." The `citations` event resolved marker 1 to the right
  document_id, chunk_id, page and snippet.
- History round-trips in the right order (question above its own
  answer), which is the `_TURN_ORDER` + migration 0007 fix holding on
  real rows written in one transaction.
- DB check: the assistant row carries `kind='answer'` with
  `cited_chunk_ids` set, and the user row carries `kind=NULL`, exactly
  as designed.
- **Noted, not changed:** `MessageResponse` does not expose `kind`, so a
  client cannot tell a refusal or an error from an answer. The data is
  right and `list_messages` returns the rows; whether the API should
  surface the marker is a product decision, not a bug.
- Harness friction worth remembering: `/auth/register` rejects
  `.test` email domains (email-validator treats reserved TLDs as
  invalid), upload returns `document_id` rather than `id`, and Celery on
  Windows needs `--pool=solo`.

Phase 3 modules:
- `retrieval.py` - one org-scoped path: HNSW cosine + Postgres FTS on
  children, merged with RRF (k=60). `SET LOCAL hnsw.iterative_scan` for the
  ANN+filter recall problem.
- `reranker.py` - async hosted/passthrough/fake, timeout -> RRF fallback,
  children -> parents deduped by best rank, confidence gate.
- `prompting.py` / `llm.py` / `citations.py` - numbered `[n] (file, p.X)`
  blocks inside unforgeable delimiters, no tools on the LLM, markers
  resolved against the server-built block set.
- `query_rewrite.py` / `cache.py` - rewrite only when history exists; cache
  key = sha256(org_id | normalized rewritten question | index_version).
- `query_service.py` - three phases; no DB session held during streaming.
- `conversations.py` route - SSE token events then one citations event.

### Measured finding on the ANN+filter recall problem
At realistic sizes the Postgres planner does NOT choose HNSW for a small
tenant (12 of 20k rows -> it uses the `org_id` btree + sort, which has
perfect recall). The ANN failure mode only appears once the filter stops
being selective enough for that path. Measured with the ANN path forced
(`enable_seqscan=off`, `enable_sort=off`) on a 30k-row corpus:

| tenant share | iterative_scan=off | relaxed_order |
|---|---|---|
| 2000 / 32k (6%) | 6 of 60 | 60 of 60 |
| 300 / 30k (1%)  | 2 of 60 | 60 of 60 |

`tests/test_retrieval.py::test_forced_ann_path_loses_recall_without_iterative_scan`
reproduces this in the suite. The plain small-tenant test passes by planner
choice, and its docstring says so.

### Local dev ports (this box)
- compose `db` -> host **5434** (local PostgreSQL 18 owns 5432 and has no
  pgvector; the collision shows up as "password authentication failed").
- compose `test-db` -> host **5433**. `redis` on 6379.
- `docker compose up -d db test-db redis` then `alembic upgrade head`.
- Another project's container holds host port 8000.

Next: Phase 4/5 are explicitly OUT OF SCOPE (eval, rate limiting,
observability, CI, deploy). CONFIDENCE_THRESHOLD calibration is the first
Phase 4 task -- see the 0.74 data point above.

Known gaps carried forward:
- `llm.py` concatenates every response part's `text` with no `thought`
  filter (two places). Latent only: Gemini does not send thought parts
  unless `thinkingConfig.includeThoughts` is set, and `thinkingBudget: 0`
  is rejected by `gemini-3.5-flash-lite` with a 400. It becomes an
  answer-corrupting bug the moment anyone enables includeThoughts.
- `MessageResponse` does not expose `messages.kind`.
- The reranker breaker clears only on process restart, which is correct
  for a config fix but means a provider-side 401 that is later resolved
  still needs a restart.
- No live test covers a refusal path (safety block, gated no-answer) or
  cache-hit behaviour end to end.

## Phase 2 Build Log

### Package rename: src/ -> app/
The repo directory was `src/`, but `organization.py` and `session.py`
already contained `from app.db.base import Base` / `from app.core.config
import settings` — imports that could never resolve. Renamed the
directory to `app/` rather than rewriting those imports, since CLAUDE.md's
own code-pattern examples (`app/services/`, `app/models/`) assume `app/`.

### Auth layer added (did not exist before this phase)
No `User` model or `get_current_user()`/admin-check dependency existed,
despite the code-pattern section assuming them. Added the minimum needed
for org-scoped, admin-gated document endpoints:
- `app/models/user.py` — `UserRole` enum (admin/member), composite
  `UniqueConstraint(org_id, email)` per the multi-tenancy decision above.
- `app/core/security.py` + `app/api/deps.py` — `get_current_user()` reads
  `sub`/`org_id` only from the verified JWT payload (never the request),
  then re-checks `org_id` against the user's live DB row. `require_admin()`
  layers a role check on top. No login/registration endpoints were added —
  out of scope for this ticket.

### Models + migration (Document, DocumentChunk)
- `DocumentChunk.org_id` is denormalized from `documents.org_id` rather
  than joined at query time — similarity search hits this table directly
  on the hot retrieval path, so the tenant filter can't depend on a join.
- `parent_id` self-references `document_chunks.id` (NULL = parent, non-null
  = child) instead of two separate tables.
- Two migrations instead of one: `0001` creates `organizations`/`users`
  (the Phase-1 baseline that had never actually been migrated into the
  DB), `0002` creates `documents`/`document_chunks` + the pgvector
  extension + an ivfflat index using `vector_cosine_ops` (matches how
  OpenAI embeddings are compared at query time — cosine, not L2) with
  `lists=100` as a cold-table default to re-tune as row volume grows.

### Celery + ingestion task
- `task_acks_late=True` + `worker_prefetch_multiplier=1` on the Celery app:
  the ingestion task is idempotent by design (wipes and redoes its own
  chunks), so a crashed worker should get the task *redelivered*, not
  silently dropped.
- Chunking algorithm lives in `app/services/chunking.py`, decoupled from
  Celery/DB — a line-by-line state machine groups text into table/code/
  paragraph blocks first, then packs blocks into ~500-token parents
  (never splitting a block), then slides a 125-token/20-token-overlap
  window over each parent's token stream for children. **Verified by
  actually running it** (not just reviewed) against a sample page with a
  table, a code fence, and a heading — confirmed the table and code fence
  each stayed intact in one block, headings started a fresh parent, and
  sibling children have exactly the specified 20-token overlap.
- Retry semantics: Celery's `self.retry()` raises either `Retry`
  (reschedule) or `MaxRetriesExceededError` (exhausted) — both are
  `Exception` subclasses. The task explicitly re-raises `Retry` before its
  catch-all, otherwise every retry attempt (not just the final failure)
  would incorrectly mark the document `failed`. Embedding-call retries use
  `countdown = 5 * (2 ** retry_count)` as specified.
- `.pdf` is extracted page-by-page via PyMuPDF as specified; `.txt`/`.md`
  are read as a single synthetic page, since those formats have no page
  concept — faking pages for them would be dishonest.

### Endpoints
- `GET /documents/{id}` returns 404 (not 403) for a document in another
  org: a 403 would confirm the id exists somewhere, letting someone probe
  ids across tenants; 404 is indistinguishable from "never existed." The
  org filter is part of the query itself (`WHERE id = ? AND org_id = ?`),
  not a fetch-then-reject check.
- Uploaded files are stored as `{document_id}{ext}`, not the user-supplied
  filename — avoids path traversal from a crafted filename and collisions.

### Environment fix: Python 3.14 dependency pins
The dev venv runs Python 3.14.5 (very new). Several original pins had no
cp314 wheel and failed to build from source without a working MSVC
linker: `pymupdf` 1.24.10 -> 1.28.2, `sqlalchemy` 2.0.35 -> 2.0.54 (2.0.35
also crashes on `Mapped[Optional[...]]` columns under 3.14's typing
internals — found by actually importing the app, not by inspection),
`psycopg2-binary` 2.9.9 -> 2.9.13, `pydantic`/`pydantic-settings`/
`email-validator`/`tiktoken` bumped to match what was already installed.
`bcrypt==4.0.1` was deliberately left untouched and verified to still work
(it ships an abi3 wheel, portable across Python versions) — it's the one
pin CLAUDE.md explicitly calls a non-negotiable, paired with
`passlib==1.7.4`.

### Tests
All 4 required tests pass against the real installed stack (SQLAlchemy,
pydantic, FastAPI — not mocks) using an in-memory SQLite DB swapped in via
`get_db` override; `ingest_document.delay()` is monkeypatched to a no-op
so the suite needs no running Redis/Celery worker. SQLite means these
tests don't exercise pgvector-specific behavior (ivfflat, cosine
distance) — that would need a separate integration test against real
Postgres+pgvector.


## Added decisions
- Postgres image: pgvector/pgvector:pg16. Tests run against real Postgres, not SQLite.
- Child chunks searched via HNSW (cosine) + Postgres full-text (tsvector/GIN), merged with RRF.
- Retrieved text is untrusted data. The LLM gets no tools.
- Cache key = hash(org_id + normalized rewritten question + org.index_version).
- Never hold a DB session open while streaming an LLM response.
- All external providers (embeddings, reranker, LLM) sit behind small interfaces with fake implementations for tests.
## Phase 1 Audit (2026-10-25)

### Fixed: four directed issues
1. **organizations.name is now UNIQUE** (model + migration 0003).
   Login resolves an account by (org name, email); two orgs sharing a
   name made that lookup ambiguous and whichever row Postgres returned
   first silently decided which tenant you authenticated into. Register
   returns **409** on a duplicate name -- matched on the *constraint name*,
   not on "any IntegrityError", so a NOT NULL/FK bug in register() can
   no longer hide behind a plausible-looking 409.
2. **get_current_user turns every malformed claim into 401, never 500.**
   Claim values are attacker-controlled. UUID(payload["sub"]) previously
   raised KeyError/TypeError/ValueError straight out of the dependency for
   a missing key, a non-string, or a non-UUID string. Now both claims are
   parsed inside one guarded block. decode_access_token also pins
   options={"require": ["exp", "sub"]} -- without it a token minted with
   no exp would verify forever.
3. **Tests run on real Postgres + pgvector, not SQLite.** docker-compose
   gained a disposable test-db (pgvector/pgvector:pg16, port 5433, tmpfs
   storage, fsync=off). conftest.py builds schema by running Alembic to
   head -- so the suite also proves the migrations are runnable -- and
   refuses to start if TEST_DATABASE_URL is unset or equals DATABASE_URL
   (it drops the schema). Each test is one transaction on one connection
   with join_transaction_mode="create_savepoint" and is rolled back, so
   tests cannot leak rows into each other. bcrypt==4.0.1 / passlib==1.7.4
   left untouched and verified working.
4. Suspicious-but-unchanged items are listed below.

### Fixed: three latent bugs the SQLite suite was hiding
Switching the suite to Postgres immediately failed, which is the point.
- **Migrations 0001/0002 could not run on a fresh database.** Both did
  sa.Enum(...).create(bind, checkfirst=True) and then passed the same
  sa.Enum object into create_table, which emits a *second*, unguarded
  CREATE TYPE -> DuplicateObject: type "user_role" already exists. Fixed
  by referencing the type as postgresql.ENUM(..., create_type=False) in
  create_table. These migrations had evidently never run on Postgres.
- **The ORM persisted enum *names*, the DB type holds enum *values*.**
  SAEnum(UserRole) sends 'MEMBER'; the Postgres type accepts 'member'.
  Every user/document insert failed with
  invalid input value for enum user_role: "MEMBER". Fixed with
  values_callable=lambda e: [m.value for m in e] on both enums. SQLite
  stores either string happily, which is exactly why this survived.
- **org.id was None when read for user.org_id.** default=uuid.uuid4 on a
  column fires at INSERT, not at object construction, so
  Organization(name=...) then User(org_id=org.id) wrote NULL. Fixed by
  generating both ids explicitly in auth_service.register.

### Also fixed
- alembic/env.py overwrote sqlalchemy.url from settings.DATABASE_URL
  unconditionally, so the test harness's URL was ignored and migrations
  ran against **dev**. Now it only fills in that default when the caller
  supplied no URL.
- .gitignore did not cover venv/ (this repo's dev venv), uploads/*, or
  *.sqlite3.

### Flagged, deliberately NOT changed
Real issues, but they belong to later parts or to a decision the project
has not made yet. Listed so none of them is a surprise later.
- **ingestion.py holds an open DB transaction across N embedding network
  calls.** It flushes parent rows, loops embedding batches, then commits.
  A slow or hanging OpenAI call keeps a write transaction (and its locks)
  open for the duration. Phase 2 PART 4 reverses this: embed everything in
  memory first, then one transaction.
- **self.retry() on a mid-stream batch failure re-runs the whole task**,
  re-paying for every batch already embedded. PART 4 retries only the
  failed batch.
- **Model names and batch sizes are hardcoded** in ingestion.py
  (EMBEDDING_MODEL, EMBEDDING_BATCH_SIZE) and chunking.py
  (encoding_for_model("text-embedding-3-small"), the token constants).
  Phase 2 moves all of them into Settings.
- **document_chunks has no embedding_model column**, so there is no way to
  tell which model produced a stored vector, making a model migration a
  guess. Added in PART 1.
- **The ivfflat index is built on an empty table.** ivfflat derives
  centroids from existing rows; built at zero rows its lists are
  meaningless until a REINDEX. Phase 2 replaces it with HNSW, which builds
  incrementally.
- **Document.file_path stores a host filesystem path**, which forces API
  and worker onto a shared filesystem and cannot survive a move to object
  storage. Becomes storage_key in PART 1.
- **Upload does await file.read() with no size cap** -- a large upload is
  read entirely into memory. MAX_UPLOAD_BYTES is in Settings now but is
  not yet enforced; PART 5 enforces it.
- **FK cleanup gaps:** documents.org_id, document_chunks.org_id and
  documents.uploaded_by have no ondelete, so deleting an org or a user
  raises instead of cascading. Not fixing blind -- "what happens when an
  org is deleted" is a product decision (hard delete vs. soft).
- **No uniqueness on (document_id, parent_id, chunk_index).** Ingestion
  idempotency relies entirely on delete-then-insert; a constraint would
  make a double-write impossible rather than merely unlikely.
- **Celery's result backend is Redis but the task returns None**, so it
  accumulates dead result keys with no TTL configured.
- **app/ subpackages have no __init__.py** (implicit namespace packages).
  It works today, but it is why a typo'd module path fails as a confusing
  attribute error instead of an ImportError.
- **Swagger's Authorize button cannot work**: OAuth2PasswordBearer declares
  tokenUrl=/api/v1/auth/login, but that route takes JSON, not the OAuth2
  form body Swagger posts.
- **.env ships the placeholder JWT_SECRET_KEY=change-me-...** It is
  gitignored, so nothing leaked, but every dev box signing tokens with the
  same published default is worth noting.
con