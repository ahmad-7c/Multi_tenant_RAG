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
Phase 2 — Ingestion Pipeline: COMPLETE (models, migrations, Celery,
ingestion task, endpoints, tests — see Build Log below).
Next: define Phase 3 (retrieval/query pipeline against the chunks
ingested here).

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