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
**PHASES 1-3 COMPLETE. Free-provider migration in progress (STEP 3 of 7
done).** 396 tests green against real Postgres + pgvector, no network.
Migrations verified `downgrade base` -> `head` through 0005.

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
- STEPS 4-7 pending: Cohere reranker, connection pool, `prepare_query`
  threading test, live smoke test.

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
Phase 4 task.

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